#!/usr/bin/env python3
"""
political_flow.py - trades de la Camara de Representantes y del poder ejecutivo
(gabinete/altos cargos) de EEUU, desde una fuente publica y gratuita (sin API key),
actualizada de forma activa.

Fuente: kadoa-org/congress-trading-monitor (congress.kadoa.com), que agrega:
  - House Clerk's Financial Disclosure portal (Camara de Representantes)
  - Office of Government Ethics / OGE (poder ejecutivo: gabinete, altos cargos)
Repo: https://github.com/kadoa-org/congress-trading-monitor (MIT, datos publicos STOCK Act / Ethics in Government Act)

Nota honesta: el Senado NO esta cubierto. efdsearch.senate.gov (la fuente oficial)
tiene proteccion anti-bot desde hace tiempo y ningun dataset gratuito activo lo
scrapea ya (el que usabamos antes, senate-stock-watcher-data, esta parado desde 2020).
Si algun dia aparece una fuente fiable para el Senado, se puede anadir aqui.

Modos ("political_mode" en watchlist.json):
  "watchlist" (por defecto) -> solo trades de tickers en tu watchlist.
  "market" -> todos los trades recientes (puede ser un JSON grande).

Escribe political.json con la lista de trades mas recientes (recorte a max_rows).
Pensado para ejecutarse en GitHub Actions (necesita salida a internet).
"""
import os
import json
import datetime as dt
from datetime import date

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = ROOT
DATA_DIR = ROOT
OUT_JSON = os.path.join(DATA_DIR, "political.json")

UA = {"User-Agent": "OpScan/1.0 (radar educativo; contacto opscan@example.com)"}

TRADES_URL = "https://raw.githubusercontent.com/kadoa-org/congress-trading-monitor/main/public/data/trades.json"

DEFAULTS = {
    "political_mode": "watchlist",     # "watchlist" o "market"
    "political_horizon_days": 180,     # solo trades de los ultimos N dias
    "political_max_rows": 300,         # recorte del JSON final
    "political_include_executive": True,  # incluir gabinete/altos cargos (OGE), no solo Camara
    "tickers": [],
}


def load_config():
    cfg = dict(DEFAULTS)
    path = os.path.join(CONFIG_DIR, "watchlist.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        for k in DEFAULTS:
            if user.get(k) is not None:
                cfg[k] = user[k]
        cfg["tickers"] = [t.strip().upper() for t in user.get("tickers", [])]
    return cfg


def _get(url, timeout=60):
    import requests
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r


def days_since(iso):
    try:
        d = dt.date.fromisoformat(iso[:10])
        return (date.today() - d).days
    except Exception:
        return None


def _direction(type_raw):
    t = (type_raw or "").lower()
    if "purchase" in t or t == "buy":
        return "Purchase"
    if "sale" in t or "sell" in t:
        return "Sale"
    if "exchange" in t:
        return "Exchange"
    return "Otro"


def _chamber_label(rec):
    branch = rec.get("branch")
    chamber = rec.get("chamber")
    if chamber == "house":
        return "Camara"
    if chamber == "senate":
        return "Senado"
    if branch == "executive":
        return "Ejecutivo"
    return chamber or branch or "—"


def fetch_trades(cfg):
    out = []
    data = _get(TRADES_URL, timeout=90).json()
    for r in data:
        try:
            branch = r.get("branch")
            if branch == "executive" and not cfg["political_include_executive"]:
                continue
            iso = (r.get("transaction_date") or "")[:10]
            if not iso:
                continue
            tk = (r.get("ticker") or "").strip().upper()
            if not tk or tk in ("--", "N/A", ""):
                continue
            out.append({
                "chamber": _chamber_label(r),
                "member": r.get("filer_name") or "—",
                "office": r.get("office") or "",
                "ticker": tk,
                "company": r.get("asset_name") or tk,
                "type_raw": r.get("transaction_type") or "",
                "direction": _direction(r.get("transaction_type")),
                "amount": r.get("amount_range_label") or "—",
                "transaction_date": iso,
                "filing_date": (r.get("filing_date") or "")[:10] or None,
                "link": r.get("doc_url"),
            })
        except Exception:
            continue
    return out


def collect(cfg):
    print("  - House Clerk + OGE (kadoa-org/congress-trading-monitor) ...")
    try:
        recs = fetch_trades(cfg)
        print(f"    {len(recs)} registros")
        return recs
    except Exception as e:
        print(f"    ERROR: {e}")
        return []


def finalize(records, cfg):
    horizon = cfg["political_horizon_days"]
    wl = set(cfg["tickers"])
    clean, seen = [], set()
    for r in records:
        d = days_since(r["transaction_date"])
        if d is None or d < 0 or d > horizon:
            continue
        if cfg["political_mode"] == "watchlist" and r["ticker"] not in wl:
            continue
        key = (r["transaction_date"], r["member"], r["ticker"], r["type_raw"], r["amount"])
        if key in seen:
            continue
        seen.add(key)
        r["days"] = d
        clean.append(r)
    clean.sort(key=lambda x: x["transaction_date"], reverse=True)
    return clean[: cfg["political_max_rows"]]


def main():
    cfg = load_config()
    print(f"Political Flow - modo={cfg['political_mode']} horizonte={cfg['political_horizon_days']}d")
    final = finalize(collect(cfg), cfg)
    by_ticker = {}
    for r in final:
        by_ticker[r["ticker"]] = by_ticker.get(r["ticker"], 0) + 1
    out = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "mode": cfg["political_mode"],
        "horizon_days": cfg["political_horizon_days"],
        "count": len(final),
        "by_ticker": by_ticker,
        "note": "Cubre Camara de Representantes y poder ejecutivo (OGE). El Senado no tiene fuente gratuita activa (efdsearch.senate.gov bloquea scraping).",
        "trades": final,
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"Escrito {OUT_JSON}: {len(final)} trades")


if __name__ == "__main__":
    main()
