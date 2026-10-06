"""Escaneo del MERCADO COMPLETO (todos los valores con opciones, ~5.300) una vez al dia.

No sustituye al escaneo principal (~960 valores cada 30 min): busca ACTIVIDAD ANOMALA en el
resto del mercado -el caso AIT: 795 calls en un valor que tenia 41 contratos abiertos- y
pasa esos valores "calientes" al escaneo principal durante unos dias, donde se siguen de cerca
(incluida la confirmacion por OI al dia siguiente).

Salida (rama `amplio`):
  hot.json      valores calientes (los lee el escaneo principal)
  history.json  historial de detecciones (30 dias)
"""
import argparse
import csv
import datetime as dt
import io
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import options
from .util import get, iso_now, log, read_json, today_et, write_json

SYMBOL_DIR = "https://www.cboe.com/us/options/symboldir/equity_index_options/?download=csv"

# criterios de actividad anomala
MIN_PREMIUM = 25_000          # dinero minimo en el contrato
MIN_VOLUME = 100              # contratos minimos
MIN_VOL_OI = 5                # volumen >= 5x lo que habia abierto en ese contrato
REL_TICKER = 1.0              # ...y >= todo el OI del valor (en valores poco negociados)
BIG_PREMIUM = 250_000         # o, sin la condicion anterior, si es mucho dinero
HOT_DAYS = 5                  # dias que un valor sigue "caliente" en el escaneo principal


def load_symbols():
    """Listado oficial de Cboe: todos los valores con opciones."""
    txt = get(SYMBOL_DIR, headers={"User-Agent": "Mozilla/5.0 OpScan"}, timeout=60).text
    out = []
    for row in csv.DictReader(io.StringIO(txt), skipinitialspace=True):
        s = (row.get("Stock Symbol") or "").strip().upper()
        if s and s.replace(".", "").replace("/", "").isalpha() and len(s) <= 6:
            out.append({"ticker": s.replace("/", "."), "name": (row.get("Company Name") or "").strip()})
    return out


def detect(und, contracts, name=""):
    """Contratos con actividad anomala. Devuelve None si no hay nada destacable."""
    spot = und.get("price")
    tot_oi = sum(c["oi"] for c in contracts)
    tot_vol = sum(c["volume"] for c in contracts)
    cands = []
    for c in contracts:
        if c["dte"] < 2 or c["volume"] < MIN_VOLUME:
            continue
        px = options.trade_price(c)
        prem = c["volume"] * px * 100
        if prem < MIN_PREMIUM:
            continue
        vol_oi = c["volume"] / max(c["oi"], 1)
        rel = c["volume"] / max(tot_oi, 1)
        if vol_oi < MIN_VOL_OI or not (rel >= REL_TICKER or prem >= BIG_PREMIUM):
            continue
        side = options.estimate_side(c)
        mid = (c["bid"] + c["ask"]) / 2 if c["ask"] > 0 else 0
        spread = (c["ask"] - c["bid"]) / mid if mid > 0 and c["bid"] > 0 else None
        otm = None
        if spot:
            otm = (c["strike"] / spot - 1) if c["kind"] == "C" else (1 - c["strike"] / spot)
        cands.append({"symbol": c["symbol"], "kind": "CALL" if c["kind"] == "C" else "PUT", "strike": c["strike"],
                      "expiration": c["expiration"], "dte": c["dte"], "volume": int(c["volume"]),
                      "oi": int(c["oi"]), "price": round(px, 2), "premium": round(prem),
                      "vol_oi": round(vol_oi, 1), "x_ticker_oi": round(rel, 1), "side": side,
                      "otm_pct": round(otm * 100, 1) if otm is not None else None,
                      "bid": c["bid"], "ask": c["ask"],
                      "spread_pct": round(spread * 100) if spread is not None else None})
    if not cands:
        return None
    cands.sort(key=lambda x: -x["premium"])
    top = cands[0]
    prem = sum(x["premium"] for x in cands)
    calls = sum(x["premium"] for x in cands if x["kind"] == "CALL")
    bias = (2 * calls - prem) / prem if prem else 0
    level = "ALTA" if (prem >= BIG_PREMIUM or top["x_ticker_oi"] >= 10) else "MEDIA"
    sp = top.get("spread_pct")
    liq = "buena" if sp is not None and sp <= 10 else ("aceptable" if sp is not None and sp <= 25 else "mala")
    why = (f"{top['volume']:,} {top['kind'].lower()}s {top['strike']:g} ({top['expiration']}) = "
           f"{top['x_ticker_oi']:g}x todo el OI del valor; ${top['premium']/1e3:,.0f}k").replace(",", ".")
    return {"ticker": und["ticker"], "name": name, "price": spot, "change_pct": und.get("change_pct"),
            "session_date": und.get("session_date"), "total_volume": int(tot_vol), "total_oi": int(tot_oi),
            "premium": prem, "contracts": cands[:5], "n": len(cands),
            "direction": "ALCISTA" if bias > 0.3 else ("BAJISTA" if bias < -0.3 else "MIXTO"),
            "level": level, "liquidity": liq, "why": why,
            "score": round(min(100, 20 * math.log10(1 + prem / MIN_PREMIUM) + min(30, 3 * top["x_ticker_oi"])
                               + (10 if (top.get("otm_pct") or -1) >= 3 else 0)
                               + (8 if top["side"] == "ASK" else 0)), 1)}


def _scan_one(t):
    payload = options.fetch_cboe(t["ticker"])
    und, contracts = options.parse_cboe(payload, t["ticker"])
    if not contracts or not und.get("price"):
        return None
    return detect(und, contracts, t.get("name", ""))


def scan(symbols, skip=(), workers=16, max_seconds=4800):
    skip = set(skip)
    todo = [s for s in symbols if s["ticker"] not in skip]
    found, errors, t0 = [], 0, time.time()
    log(f"amplio: {len(todo)} valores fuera del universo principal")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_scan_one, s): s for s in todo}
        for i, f in enumerate(as_completed(futs), 1):
            try:
                r = f.result()
                if r:
                    found.append(r)
            except Exception:
                errors += 1
            if i % 500 == 0:
                log(f"  amplio: {i}/{len(todo)} ({time.time()-t0:.0f}s), {len(found)} anomalias")
            if time.time() - t0 > max_seconds:
                log("amplio: tiempo maximo alcanzado, se corta")
                ex.shutdown(wait=False, cancel_futures=True)   # sin esto seguiria con los pendientes
                break
    found.sort(key=lambda x: -x["score"])
    return found, {"scanned": len(todo), "errors": errors, "secs": round(time.time() - t0)}


def update_hot(prev_hot, found, today=None, days=HOT_DAYS):
    """Valores calientes: los detectados hoy + los de los ultimos dias (para seguir la confirmacion)."""
    today = today or today_et()
    keep_from = (today - dt.timedelta(days=days + 2)).isoformat()
    hot = {h["ticker"]: h for h in (prev_hot or []) if h.get("first_seen", "") >= keep_from}
    for f in found:
        h = hot.get(f["ticker"])
        hot[f["ticker"]] = {"ticker": f["ticker"], "name": f.get("name"), "level": f["level"],
                            "first_seen": h["first_seen"] if h else today.isoformat(),
                            "last_seen": today.isoformat(), "why": f["why"], "direction": f["direction"],
                            "score": f["score"], "liquidity": f["liquidity"]}
    return sorted(hot.values(), key=lambda x: (x["last_seen"], x["score"]), reverse=True)


MAX_HOT = 60                  # como mucho 60 valores extra en el escaneo principal (no lo ralentiza)


def load_hot(path, today=None, days=HOT_DAYS, limit=MAX_HOT):
    """Para el escaneo principal: tickers calientes vigentes (los de mas puntuacion). Nunca falla."""
    try:
        if not path or not os.path.exists(path):
            return []
        today = today or today_et()
        since = (today - dt.timedelta(days=days + 2)).isoformat()
        hot = [h for h in (read_json(path, {}) or {}).get("hot", []) if h.get("last_seen", "") >= since
               and h.get("ticker")]
        return sorted(hot, key=lambda h: -(h.get("score") or 0))[:limit]
    except Exception as e:
        log(f"amplio: no se pudo leer hot.json ({e}); se sigue sin calientes")
        return []


def main(argv=None):
    p = argparse.ArgumentParser(prog="opscan.amplio")
    p.add_argument("--out", default="amplio")
    p.add_argument("--core", default="data/latest.json", help="latest.json del escaneo principal (para no repetir)")
    p.add_argument("--workers", type=int, default=16)
    a = p.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    core = {r["ticker"] for r in (read_json(a.core, {}) or {}).get("records", [])}
    symbols = load_symbols()
    found, stats = scan(symbols, skip=core, workers=a.workers)
    prev = read_json(os.path.join(a.out, "hot.json"), {}) or {}
    hot = update_hot(prev.get("hot"), found)
    write_json(os.path.join(a.out, "hot.json"), {"generated_at": iso_now(), "universe": len(symbols),
                                                 "core": len(core), "stats": stats, "found": found[:300],
                                                 "hot": hot})
    hist = read_json(os.path.join(a.out, "history.json"), {"days": []}) or {"days": []}
    hist["days"] = [d for d in hist["days"] if d["date"] != today_et().isoformat()][-29:] + [
        {"date": today_et().isoformat(), "n": len(found), "tickers": [f["ticker"] for f in found[:100]]}]
    write_json(os.path.join(a.out, "history.json"), hist)
    log(f"amplio: {len(found)} anomalias, {len(hot)} calientes. {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
