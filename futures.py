"""Futuros: posicionamiento COT de la CFTC (semanal) + precio/volumen diario (Yahoo).

Patrones de entrada/salida (precio vs open interest, semana COT):
  precio sube + OI sube  -> entran largos nuevos   (tendencia alcista sana)
  precio sube + OI baja  -> cierre de cortos       (rebote debil)
  precio baja + OI sube  -> entran cortos nuevos   (presion bajista real)
  precio baja + OI baja  -> salen los largos       (capitulacion / posible suelo)
"""
import datetime as dt

from .util import get_json, log, num, parse_date, rnd

LEGACY = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
TFF = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"

LEGACY_FIELDS = ["report_date_as_yyyy_mm_dd", "cftc_contract_market_code", "market_and_exchange_names",
                 "open_interest_all", "noncomm_positions_long_all", "noncomm_positions_short_all",
                 "comm_positions_long_all", "comm_positions_short_all",
                 "nonrept_positions_long_all", "nonrept_positions_short_all",
                 "change_in_open_interest_all"]
TFF_FIELDS = ["report_date_as_yyyy_mm_dd", "cftc_contract_market_code",
              "lev_money_positions_long", "lev_money_positions_short",
              "asset_mgr_positions_long", "asset_mgr_positions_short"]
TFF_GROUPS = {"indices", "tipos", "divisas", "cripto"}

QUADRANTS = {
    (1, 1): ("Entrada de largos", "Precio sube y OI sube: dinero nuevo comprando", 1.0),
    (1, -1): ("Cierre de cortos", "Precio sube pero OI baja: rebote por recompra de cortos", 0.3),
    (-1, 1): ("Entrada de cortos", "Precio baja y OI sube: dinero nuevo vendiendo", -1.0),
    (-1, -1): ("Salida de largos", "Precio baja y OI baja: liquidacion, posible capitulacion", -0.3),
}


def _socrata(url, fields, codes, since):
    codes_sql = ",".join("'" + c + "'" for c in codes)
    params = {
        "$select": ",".join(fields),
        "$where": f"cftc_contract_market_code in({codes_sql}) AND report_date_as_yyyy_mm_dd >= '{since}T00:00:00'",
        "$order": "report_date_as_yyyy_mm_dd ASC",
        "$limit": 50000,
    }
    return get_json(url, params=params, timeout=60)


def _index(series, value):
    if not series:
        return None
    lo, hi = min(series), max(series)
    if hi == lo:
        return 50.0
    return round((value - lo) / (hi - lo) * 100, 1)


def analyze_cot(records, tff_records=None, weeks_long=156, weeks_short=52):
    """records: lista legacy de UN mercado (cualquier orden). Devuelve dict o None."""
    rows = sorted(records, key=lambda r: r.get("report_date_as_yyyy_mm_dd", ""))
    if not rows:
        return None
    hist = []
    for r in rows:
        spec = num(r.get("noncomm_positions_long_all")) - num(r.get("noncomm_positions_short_all"))
        comm = num(r.get("comm_positions_long_all")) - num(r.get("comm_positions_short_all"))
        small = num(r.get("nonrept_positions_long_all")) - num(r.get("nonrept_positions_short_all"))
        hist.append({"date": (r.get("report_date_as_yyyy_mm_dd") or "")[:10], "oi": num(r.get("open_interest_all")),
                     "oi_chg": num(r.get("change_in_open_interest_all"), None),
                     "spec": spec, "comm": comm, "small": small})
    last = hist[-1]
    prev = hist[-2] if len(hist) > 1 else None
    specs_l = [h["spec"] for h in hist[-weeks_long:]]
    comms_l = [h["comm"] for h in hist[-weeks_long:]]
    out = {
        "report_date": last["date"], "open_interest": int(last["oi"]),
        "oi_change": int(last["oi_chg"]) if last["oi_chg"] is not None else (int(last["oi"] - prev["oi"]) if prev else None),
        "spec_net": int(last["spec"]), "comm_net": int(last["comm"]), "small_net": int(last["small"]),
        "spec_net_change": int(last["spec"] - prev["spec"]) if prev else None,
        "comm_net_change": int(last["comm"] - prev["comm"]) if prev else None,
        "spec_pct_oi": rnd(last["spec"] / last["oi"] * 100, 1) if last["oi"] else None,
        "cot_index_spec": _index(specs_l, last["spec"]),
        "cot_index_comm": _index(comms_l, last["comm"]),
        "cot_index_spec_1y": _index([h["spec"] for h in hist[-weeks_short:]], last["spec"]),
        "history": [{"d": h["date"], "spec": int(h["spec"]), "comm": int(h["comm"]), "oi": int(h["oi"])}
                    for h in hist[-weeks_short:]],
    }
    if tff_records:
        t = sorted(tff_records, key=lambda r: r.get("report_date_as_yyyy_mm_dd", ""))
        lev = [num(r.get("lev_money_positions_long")) - num(r.get("lev_money_positions_short")) for r in t]
        am = [num(r.get("asset_mgr_positions_long")) - num(r.get("asset_mgr_positions_short")) for r in t]
        if lev:
            out["lev_money_net"] = int(lev[-1])
            out["asset_mgr_net"] = int(am[-1])
            out["lev_money_index"] = _index(lev[-weeks_long:], lev[-1])
            out["asset_mgr_index"] = _index(am[-weeks_long:], am[-1])
            out["lev_money_change"] = int(lev[-1] - lev[-2]) if len(lev) > 1 else None
    return out


def price_stats(df_close, df_vol, report_date=None, prev_report_date=None):
    """Estadisticas de precio/volumen a partir de series pandas (index fecha)."""
    c = df_close.dropna()
    if len(c) < 5:
        return None
    last = float(c.iloc[-1])
    out = {"last": rnd(last, 4), "date": str(c.index[-1])[:10],
           "chg_1d": rnd((last / float(c.iloc[-2]) - 1) * 100, 2),
           "chg_5d": rnd((last / float(c.iloc[-6]) - 1) * 100, 2) if len(c) > 6 else None,
           "chg_20d": rnd((last / float(c.iloc[-21]) - 1) * 100, 2) if len(c) > 21 else None}
    sma50 = float(c.tail(50).mean()) if len(c) >= 50 else None
    sma200 = float(c.tail(200).mean()) if len(c) >= 200 else None
    out["above_sma50"] = (last > sma50) if sma50 else None
    out["above_sma200"] = (last > sma200) if sma200 else None
    if df_vol is not None:
        v = df_vol.dropna()
        v = v[v > 0]
        if len(v) >= 21:
            avg = float(v.iloc[-21:-1].mean())
            out["vol_last"] = int(v.iloc[-1])
            out["vol_rel"] = rnd(float(v.iloc[-1]) / avg, 2) if avg > 0 else None
    # cambio de precio en la semana COT (martes a martes)
    if report_date and prev_report_date:
        try:
            p1 = c[c.index.strftime("%Y-%m-%d") <= report_date]
            p0 = c[c.index.strftime("%Y-%m-%d") <= prev_report_date]
            if len(p1) and len(p0):
                out["cot_week_chg"] = rnd((float(p1.iloc[-1]) / float(p0.iloc[-1]) - 1) * 100, 2)
        except Exception:
            pass
    # patron diario precio/volumen
    vr = out.get("vol_rel")
    if out["chg_1d"] is not None and vr is not None:
        up = out["chg_1d"] > 0
        if vr >= 1.5:
            out["daily_pattern"] = "Subida con volumen alto (entrada)" if up else "Caida con volumen alto (salida/distribucion)"
        elif vr <= 0.7:
            out["daily_pattern"] = "Subida con poco volumen (debil)" if up else "Caida con poco volumen (sin presion)"
        else:
            out["daily_pattern"] = "Volumen normal"
    return out


def classify(market, cot, px):
    """Combina cuadrante semanal, tendencia y extremos COT -> sesgo [-1, 1]."""
    reasons, bias = [], 0.0
    quad = None
    if cot and px and px.get("cot_week_chg") is not None and cot.get("oi_change") is not None:
        ps = 1 if px["cot_week_chg"] >= 0 else -1
        os_ = 1 if cot["oi_change"] >= 0 else -1
        name, desc, b = QUADRANTS[(ps, os_)]
        quad = {"name": name, "desc": desc}
        bias += 0.5 * b
        reasons.append(f"Semana COT: {name}")
    if px and px.get("above_sma50") is not None:
        bias += 0.25 if px["above_sma50"] else -0.25
        reasons.append("Precio sobre media 50d" if px["above_sma50"] else "Precio bajo media 50d")
    if cot:
        si, ci = cot.get("cot_index_spec"), cot.get("cot_index_comm")
        if si is not None and si >= 90:
            bias -= 0.2; reasons.append(f"Especuladores muy largos (COT idx {si}) - riesgo de giro")
        elif si is not None and si <= 10:
            bias += 0.2; reasons.append(f"Especuladores muy cortos (COT idx {si}) - posible rebote")
        if ci is not None and ci >= 80:
            bias += 0.25; reasons.append(f"Commercials muy largos (idx {ci}) - dinero 'listo' comprando")
        elif ci is not None and ci <= 20:
            bias -= 0.25; reasons.append(f"Commercials muy cortos (idx {ci})")
    if px and px.get("daily_pattern") and "volumen alto" in px["daily_pattern"]:
        reasons.append("Hoy: " + px["daily_pattern"])
        bias += 0.1 if "Subida" in px["daily_pattern"] else -0.1
    bias = max(-1.0, min(1.0, bias))
    label = "ALCISTA" if bias > 0.25 else ("BAJISTA" if bias < -0.25 else "NEUTRAL")
    return {"bias": round(bias, 2), "label": label, "quadrant": quad, "reasons": reasons}


def fetch_prices(symbols):
    import yfinance as yf
    df = yf.download(symbols, period="2y", interval="1d", group_by="column",
                     auto_adjust=False, progress=False, threads=True)
    return df


def build(cfg, status):
    fc = cfg["futures"]
    markets = fc.get("markets") or []
    if not markets:
        return {"markets": [], "regime": None}
    since = (dt.date.today() - dt.timedelta(days=int(fc.get("cot_years", 3)) * 365 + 14)).isoformat()
    codes = [m["code"] for m in markets if m.get("code")]
    legacy, tff = {}, {}
    try:
        for r in _socrata(LEGACY, LEGACY_FIELDS, codes, since):
            legacy.setdefault(r["cftc_contract_market_code"].strip(), []).append(r)
        status.ok("CFTC COT (legacy)", sum(len(v) for v in legacy.values()))
    except Exception as e:
        status.fail("CFTC COT (legacy)", e)
    try:
        tcodes = [m["code"] for m in markets if m.get("group") in TFF_GROUPS]
        for r in _socrata(TFF, TFF_FIELDS, tcodes, since):
            tff.setdefault(r["cftc_contract_market_code"].strip(), []).append(r)
        status.ok("CFTC COT (TFF)", sum(len(v) for v in tff.values()))
    except Exception as e:
        status.fail("CFTC COT (TFF)", e)

    prices = None
    try:
        prices = fetch_prices([m["yf"] for m in markets if m.get("yf")])
        status.ok("Yahoo futuros", len(markets))
    except Exception as e:
        status.fail("Yahoo futuros", e)

    out = []
    for m in markets:
        cot = analyze_cot(legacy.get(m["code"], []), tff.get(m["code"]))
        px = None
        if prices is not None and m.get("yf"):
            try:
                close = prices["Close"][m["yf"]] if "Close" in prices else None
                vol = prices["Volume"][m["yf"]] if "Volume" in prices else None
                prev_rd = cot["history"][-2]["d"] if cot and len(cot["history"]) > 1 else None
                px = price_stats(close, vol, cot["report_date"] if cot else None, prev_rd)
            except Exception as e:
                log(f"futuros {m['key']}: {e}")
        cls = classify(m, cot, px)
        if not cot:
            cls["reasons"].append("Sin datos COT para este codigo")
        out.append({"key": m["key"], "name": m["name"], "group": m.get("group"), "yf": m.get("yf"),
                    "code": m.get("code"), "cot": cot, "price": px, **cls})
    return {"markets": out, "regime": market_regime(out)}


def market_regime(markets):
    by = {m["key"]: m for m in markets}
    vals = [by[k]["bias"] for k in ("ES", "NQ", "RTY") if k in by]
    if not vals:
        return None
    b = sum(vals) / len(vals)
    if "VX" in by:
        b -= 0.3 * by["VX"]["bias"]   # VIX al alza = viento en contra para la renta variable
    b = max(-1.0, min(1.0, b))
    return {"bias": round(b, 2), "label": "ALCISTA" if b > 0.2 else ("BAJISTA" if b < -0.2 else "NEUTRAL")}


def sector_context(futures, sector, links):
    """Sesgo de contexto de futuros para un sector: regimen general + futuros ligados."""
    if not futures:
        return None, []
    by = {m["key"]: m for m in futures.get("markets", [])}
    parts, why = [], []
    reg = futures.get("regime")
    if reg:
        parts.append(reg["bias"])
        why.append(f"Indices {reg['label'].lower()}")
    for k in (links or {}).get(sector, []):
        sign = -1 if str(k).startswith("-") else 1
        key = str(k).lstrip("-")
        if key in by:
            parts.append(sign * by[key]["bias"])
            why.append(f"{by[key]['name']} {by[key]['label'].lower()}" + (" (inverso)" if sign < 0 else ""))
    if not parts:
        return None, []
    return round(sum(parts) / len(parts), 2), why
