#!/usr/bin/env python3
"""
political_flow.py - trades de congresistas de EEUU (Senado + Camara) desde fuentes
publicas y gratuitas (sin API key), basadas en los disclosures obligatorios del
STOCK Act.

Fuentes:
  - Senado: senate-stock-watcher-data (raw.githubusercontent.com), mismo dato que
    senatestockwatcher.com. Cada registro trae senador, ticker, tipo, importe (rango) y fecha.
  - Camara: house-stock-watcher-data (S3), mismo dato que housestockwatcher.com.

Modos ("political_mode" en watchlist.json):
  "watchlist" (por defecto) -> solo trades de tickers en tu watchlist.
  "market" -> todos los trades recientes (puede ser un JSON grande).

Escribe political.json con la lista de trades mas recientes (recorte a max_rows).
Pensado para ejecutarse en GitHub Actions (necesita salida a internet).
"""
import os
import json
import datetime as dt
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = ROOT
DATA_DIR = ROOT
OUT_JSON = os.path.join(DATA_DIR, "political.json")

UA = {"User-Agent": "OpScan/1.0 (radar educativo; contacto opscan@example.com)"}

SENATE_URL = "https://raw.githubusercontent.com/timothycarambat/senate-stock-watcher-data/master/aggregate/all_transactions.json"
HOUSE_URL = "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json"

DEFAULTS = {
    "political_mode": "watchlist",   # "watchlist" o "market"
    "political_horizon_days": 180,   # solo trades de los ultimos N dias
    "political_max_rows": 300,       # recorte del JSON final
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


def _iso_from_mdy(s):
    """Convierte MM/DD/YYYY -> YYYY-MM-DD. Si ya viene en ISO, lo deja igual."""
    if not s:
        return None
    s = str(s).strip()
    if len(s) >= 10 and s[4] == "-":
        return s[:10]
    try:
        m, d, y = s.split("/")
        return f"{y}-{int(m):02d}-{int(d):02d}"
    except Exception:
        return None


def days_since(iso):
    try:
        d = dt.date.fromisoformat(iso)
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


def _get_any(d, keys, default=None):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d.get(k)
    return default


# --------------------------------------------------------------------------
# Senado
# --------------------------------------------------------------------------
def fetch_senate():
    out = []
    data = _get(SENATE_URL).json()
    for r in data:
        try:
            iso = _iso_from_mdy(r.get("transaction_date"))
            if not iso:
                continue
            tk = (r.get("ticker") or "").strip().upper()
            if not tk or tk == "--":
                continue
            out.append({
                "chamber": "Senado",
                "member": r.get("senator") or "—",
                "ticker": tk,
                "company": r.get("asset_description") or tk,
                "asset_type": r.get("asset_type") or "Stock",
                "type_raw": r.get("type") or "",
                "direction": _direction(r.get("type")),
                "amount": r.get("amount") or "—",
                "transaction_date": iso,
                "link": r.get("ptr_link"),
            })
        except Exception:
            continue
    return out


# --------------------------------------------------------------------------
# Camara de Representantes
# --------------------------------------------------------------------------
def fetch_house():
    out = []
    data = _get(HOUSE_URL, timeout=90).json()
    for r in data:
        try:
            raw_date = _get_any(r, ["transaction_date", "transactionDate"])
            iso = _iso_from_mdy(raw_date)
            if not iso:
                continue
            tk = (r.get("ticker") or "").strip().upper()
            if not tk or tk in ("--", "N/A", ""):
                continue
            member = _get_any(r, ["representative", "member", "name"], "—")
            out.append({
                "chamber": "Camara",
                "member": member,
                "ticker": tk,
                "company": r.get("asset_description") or tk,
                "asset_type": r.get("asset_type") or "Stock",
                "type_raw": r.get("type") or "",
                "direction": _direction(r.get("type")),
                "amount": r.get("amount") or "—",
                "transaction_date": iso,
                "link": r.get("ptr_link"),
            })
        except Exception:
            continue
    return out


def collect():
    records = []
    for name, fn in (("Senado", fetch_senate), ("Camara", fetch_house)):
        print(f"  - {name} ...")
        try:
            recs = fn()
            print(f"    {len(recs)} registros")
            records += recs
        except Exception as e:
            print(f"    ERROR en {name}: {e}")
    return records


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
    final = finalize(collect(), cfg)
    by_ticker = {}
    for r in final:
        by_ticker[r["ticker"]] = by_ticker.get(r["ticker"], 0) + 1
    out = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "mode": cfg["political_mode"],
        "horizon_days": cfg["political_horizon_days"],
        "count": len(final),
        "by_ticker": by_ticker,
        "trades": final,
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"Escrito {OUT_JSON}: {len(final)} trades")


if __name__ == "__main__":
    main()
