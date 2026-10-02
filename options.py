"""Cadenas de opciones (TODOS los vencimientos) y deteccion de flujo inusual.

Fuente principal: JSON publico retrasado de Cboe (una sola peticion por valor devuelve
todos los vencimientos con volumen, OI, IV, griegas y bid/ask).
Respaldo: yfinance (una peticion por vencimiento; griegas calculadas con Black-Scholes).

Las funciones parse_* / analyze_* son puras (sin red) y estan cubiertas por tests.
"""
import datetime as dt
import math
import re

from .util import (HttpError, get_json, num, parse_date, rnd, safe_ratio, third_friday,
                   today_et)

CBOE_URLS = [
    "https://cdn-api.cboe.com/api/global/delayed_quotes/options/{sym}.json",
    "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json",
]
INDEXES = {"SPX", "NDX", "VIX", "RUT", "XSP", "DJX", "OEX", "XEO", "MRUT"}

OCC_RE = re.compile(r"^(?P<root>[A-Z0-9.]+?)(?P<date>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")


# ------------------------------------------------------------------ simbolos
def cboe_symbol(ticker):
    t = ticker.upper().lstrip("^")
    return "_" + t if t in INDEXES else t


def yf_symbol(ticker):
    t = ticker.upper()
    if t in INDEXES:
        return "^" + t
    return t.replace(".", "-")


def parse_occ(sym):
    """'AAPL260116C00150000' -> ('AAPL', date(2026,1,16), 'C', 150.0)."""
    m = OCC_RE.match((sym or "").strip().upper())
    if not m:
        return None
    d = m.group("date")
    try:
        exp = dt.date(2000 + int(d[:2]), int(d[2:4]), int(d[4:6]))
    except ValueError:
        return None
    return m.group("root"), exp, m.group("cp"), int(m.group("strike")) / 1000.0


# ------------------------------------------------------------------ Black-Scholes
def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _npdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def bs_greeks(kind, spot, strike, t_years, iv, r=0.04):
    """delta y gamma de Black-Scholes. Devuelve (None, None) si faltan datos."""
    if not spot or not strike or not iv or iv <= 0 or t_years <= 0:
        return None, None
    try:
        sq = iv * math.sqrt(t_years)
        d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / sq
        gamma = _npdf(d1) / (spot * sq)
        delta = _ncdf(d1) if kind == "C" else _ncdf(d1) - 1.0
        return delta, gamma
    except (ValueError, ZeroDivisionError):
        return None, None


# ------------------------------------------------------------------ descarga
def fetch_cboe(ticker):
    sym = cboe_symbol(ticker)
    last = None
    for tpl in CBOE_URLS:
        try:
            return get_json(tpl.format(sym=sym), timeout=30, retries=2)
        except HttpError as e:
            last = e
            if e.status == 404 or e.status == 403:
                continue
    raise last or HttpError("?", sym)


def parse_cboe(payload, ticker, today=None):
    """Normaliza el JSON de Cboe -> (subyacente, contratos)."""
    today = today or today_et()
    data = (payload or {}).get("data") or {}
    und = {
        "ticker": ticker.upper(),
        "price": num(data.get("current_price"), None) or num(data.get("close"), None)
                 or num(data.get("prev_day_close"), None),
        "prev_close": num(data.get("prev_day_close"), None),
        "change_pct": rnd(data.get("price_change_percent"), 2),
        "stock_volume": num(data.get("volume"), None),
        "iv30": (num(data.get("iv30"), 0) / 100.0) or None,
        "iv30_change": rnd(num(data.get("iv30_change"), 0) / 100.0, 4) if data.get("iv30_change") is not None else None,
        "last_trade_time": data.get("last_trade_time"),
        "source": "Cboe",
    }
    sess = parse_date(data.get("last_trade_time")) or parse_date(payload.get("timestamp")) or today
    und["session_date"] = sess.isoformat()
    contracts = []
    for o in data.get("options") or []:
        p = parse_occ(o.get("option"))
        if not p:
            continue
        _, exp, cp, strike = p
        contracts.append({
            "symbol": o.get("option"), "kind": cp, "expiration": exp.isoformat(),
            "dte": (exp - sess).days, "strike": strike,
            "bid": num(o.get("bid")), "ask": num(o.get("ask")),
            "last": num(o.get("last_trade_price")), "volume": num(o.get("volume")),
            "oi": num(o.get("open_interest")), "iv": num(o.get("iv")),
            "delta": num(o.get("delta"), None), "gamma": num(o.get("gamma"), None),
            "last_trade_time": o.get("last_trade_time"),
        })
    return und, contracts


def fetch_yfinance(ticker, max_dte=800, today=None):
    """Respaldo con yfinance: todas las expiraciones <= max_dte."""
    import yfinance as yf
    today = today or today_et()
    t = yf.Ticker(yf_symbol(ticker))
    price = None
    try:
        price = float(t.fast_info["last_price"])
    except Exception:
        h = t.history(period="5d")
        if len(h):
            price = float(h["Close"].iloc[-1])
    und = {"ticker": ticker.upper(), "price": price, "prev_close": None, "change_pct": None,
           "stock_volume": None, "iv30": None, "iv30_change": None, "last_trade_time": None,
           "source": "Yahoo", "session_date": today.isoformat()}
    contracts = []
    for e in list(t.options or []):
        exp = parse_date(e)
        if not exp:
            continue
        dte = (exp - today).days
        if dte < 0 or dte > max_dte:
            continue
        try:
            oc = t.option_chain(e)
        except Exception:
            continue
        for kind, df in (("C", oc.calls), ("P", oc.puts)):
            for _, r in df.iterrows():
                iv = num(r.get("impliedVolatility"))
                strike = num(r.get("strike"))
                delta, gamma = bs_greeks(kind, price, strike, max(dte, 0.5) / 365.0, iv)
                contracts.append({
                    "symbol": r.get("contractSymbol"), "kind": kind, "expiration": exp.isoformat(),
                    "dte": dte, "strike": strike, "bid": num(r.get("bid")), "ask": num(r.get("ask")),
                    "last": num(r.get("lastPrice")), "volume": num(r.get("volume")),
                    "oi": num(r.get("openInterest")), "iv": iv, "delta": delta, "gamma": gamma,
                    "last_trade_time": str(r.get("lastTradeDate") or ""),
                })
    return und, contracts


# ------------------------------------------------------------------ analisis
def mid_price(c):
    b, a = c["bid"], c["ask"]
    if b > 0 and a > 0 and a >= b:
        return (a + b) / 2.0
    return c["last"] or a or b or 0.0


def trade_price(c):
    return c["last"] if c["last"] > 0 else mid_price(c)


def estimate_side(c):
    """Lado agresor ESTIMADO comparando el ultimo precio con el bid/ask actual.
    ASK = comprado (agresivo), BID = vendido, MID = indeterminado."""
    b, a, last = c["bid"], c["ask"], c["last"]
    if last <= 0 or a <= 0 or a < b:
        return "MID"
    spread = a - b
    if spread <= 0:
        return "MID"
    if last >= a - 0.25 * spread:
        return "ASK"
    if last <= b + 0.25 * spread:
        return "BID"
    return "MID"


DIR_WEIGHTS = {
    ("C", "ASK"): ("ALCISTA", 1.0), ("C", "BID"): ("BAJISTA", 0.3), ("C", "MID"): ("NEUTRAL", 0.0),
    ("P", "ASK"): ("BAJISTA", 1.0), ("P", "BID"): ("ALCISTA", 0.3), ("P", "MID"): ("NEUTRAL", 0.0),
}


def ensure_greeks(c, spot):
    if (c.get("delta") in (None, 0) or c.get("gamma") is None) and spot and c["iv"] > 0:
        d, g = bs_greeks(c["kind"], spot, c["strike"], max(c["dte"], 0.5) / 365.0, c["iv"])
        if c.get("delta") in (None, 0):
            c["delta"] = d
        if c.get("gamma") is None:
            c["gamma"] = g


def contract_score(u, ocfg):
    s = 25 * math.log10(1 + u["premium"] / max(ocfg["min_premium"], 1))
    s = min(s, 45)
    s += min(20, 4 * u["vol_oi"])
    if u["otm"]:
        s += 10
    if u["dte"] <= 21:
        s += 8
    elif u["dte"] <= 60:
        s += 4
    if u["side"] == "ASK":
        s += 12
    elif u["side"] == "BID":
        s += 4
    if u["hedge_like"]:
        s -= 15
    if u.get("combo"):
        s -= 10
    return round(max(0, min(100, s)), 1)


def atm_iv_by_exp(contracts, spot):
    """IV ATM (media call/put del strike mas cercano con IV valida) por vencimiento."""
    by = {}
    for c in contracts:
        if c["iv"] <= 0.01 or c["iv"] > 6 or c["dte"] < 0:
            continue
        by.setdefault(c["expiration"], []).append(c)
    out = []
    for exp, cs in by.items():
        best = {}
        for c in cs:
            d = abs(c["strike"] - spot)
            if c["kind"] not in best or d < best[c["kind"]][0]:
                best[c["kind"]] = (d, c)
        ivs = [v[1]["iv"] for v in best.values()]
        if ivs:
            out.append((cs[0]["dte"], exp, sum(ivs) / len(ivs)))
    out.sort()
    return out


def interp_iv30(term):
    """Interpolacion en varianza-tiempo a 30 dias usando vencimientos >= 5 dias."""
    pts = [(d, iv) for d, _, iv in term if d >= 5]
    if not pts:
        return None
    if len(pts) == 1 or pts[0][0] >= 30:
        return pts[0][1]
    for (d1, v1), (d2, v2) in zip(pts, pts[1:]):
        if d1 <= 30 <= d2:
            w1, w2 = v1 * v1 * d1, v2 * v2 * d2
            var30 = w1 + (w2 - w1) * (30 - d1) / (d2 - d1)
            return math.sqrt(max(var30, 0) / 30)
    return pts[-1][1]


def expected_moves(contracts, spot, max_items=8):
    """Movimiento esperado por vencimiento = straddle ATM / precio."""
    by = {}
    for c in contracts:
        if c["dte"] < 0:
            continue
        by.setdefault((c["dte"], c["expiration"]), []).append(c)
    out = []
    for (dte, exp), cs in sorted(by.items())[:max_items * 2]:
        strikes = sorted({c["strike"] for c in cs}, key=lambda k: abs(k - spot))
        for k in strikes[:2]:
            call = next((c for c in cs if c["strike"] == k and c["kind"] == "C"), None)
            put = next((c for c in cs if c["strike"] == k and c["kind"] == "P"), None)
            if call and put and mid_price(call) > 0 and mid_price(put) > 0:
                em = mid_price(call) + mid_price(put)
                out.append({"expiration": exp, "dte": dte, "em_usd": rnd(em, 2),
                            "em_pct": rnd(em / spot * 100, 2)})
                break
        if len(out) >= max_items:
            break
    return out


def max_pain(contracts, expiration):
    cs = [c for c in contracts if c["expiration"] == expiration and c["oi"] > 0]
    strikes = sorted({c["strike"] for c in cs})
    if not strikes:
        return None
    best, best_k = None, None
    for k in strikes:
        pay = 0.0
        for c in cs:
            if c["kind"] == "C" and k > c["strike"]:
                pay += (k - c["strike"]) * c["oi"]
            elif c["kind"] == "P" and k < c["strike"]:
                pay += (c["strike"] - k) * c["oi"]
        if best is None or pay < best:
            best, best_k = pay, k
    return best_k


def pick_monthly(contracts, session):
    """Vencimiento mensual (3er viernes) mas cercano con contratos; si no, el de mas OI."""
    exps = sorted({c["expiration"] for c in contracts if c["dte"] >= 0})
    for e in exps:
        d = parse_date(e)
        tf = third_friday(d.year, d.month)
        if abs((d - tf).days) <= 1:
            return e
    if not exps:
        return None
    oi = {}
    for c in contracts:
        oi[c["expiration"]] = oi.get(c["expiration"], 0) + c["oi"]
    return max(exps[:6], key=lambda e: oi.get(e, 0))


def analyze_chain(und, contracts, ocfg):
    """Metricas agregadas + lista de contratos inusuales para un subyacente."""
    spot = und.get("price")
    session = und.get("session_date")
    contracts = [c for c in contracts if 0 <= c["dte"] <= ocfg["max_dte"]]
    m = {"ticker": und["ticker"], "price": rnd(spot, 2), "change_pct": und.get("change_pct"),
         "session_date": session, "source": und.get("source"),
         "n_contracts": len(contracts), "n_expirations": len({c["expiration"] for c in contracts})}
    cv = pv = coi = poi = cprem = pprem = 0.0
    bull = bear = 0.0
    gex = 0.0
    unusual = []
    call_oi_k, put_oi_k = {}, {}
    for c in contracts:
        if spot:
            ensure_greeks(c, spot)
        price = trade_price(c)
        prem = c["volume"] * price * 100
        if c["kind"] == "C":
            cv += c["volume"]; coi += c["oi"]; cprem += prem
            if c["dte"] <= 60:
                call_oi_k[c["strike"]] = call_oi_k.get(c["strike"], 0) + c["oi"]
        else:
            pv += c["volume"]; poi += c["oi"]; pprem += prem
            if c["dte"] <= 60:
                put_oi_k[c["strike"]] = put_oi_k.get(c["strike"], 0) + c["oi"]
        if spot and c.get("gamma"):
            g = c["gamma"] * c["oi"] * 100 * spot * spot * 0.01
            gex += g if c["kind"] == "C" else -g
        # ---- inusual (0DTE excluido: es trading intradia y no se puede confirmar por OI)
        if c["dte"] < ocfg.get("min_dte_unusual", 2):
            continue
        if c["volume"] < ocfg["min_volume"] or prem < ocfg["min_premium"]:
            continue
        vol_oi = c["volume"] / max(c["oi"], 1)
        if vol_oi < ocfg["min_vol_oi"]:
            continue
        side = estimate_side(c)
        delta = c.get("delta")
        hedge_like = delta is not None and abs(delta) >= ocfg["max_abs_delta_hedge"]
        otm = bool(spot) and ((c["kind"] == "C" and c["strike"] > spot) or
                              (c["kind"] == "P" and c["strike"] < spot))
        direction, w = DIR_WEIGHTS[(c["kind"], side)]
        f = 1.0                # factor estructural (independiente del lado)
        if hedge_like:
            f *= 0.3
        if c["dte"] <= 7:
            f *= 0.5          # semanales muy cortas: mucho trading intradia
        w *= f
        u = {"_w": w, "_f": f, "ticker": und["ticker"], "symbol": c["symbol"], "kind": "CALL" if c["kind"] == "C" else "PUT",
             "strike": c["strike"], "expiration": c["expiration"], "dte": c["dte"],
             "volume": int(c["volume"]), "oi": int(c["oi"]), "vol_oi": round(vol_oi, 2),
             "price": rnd(price, 2), "premium": round(prem), "iv": rnd(c["iv"], 4),
             "delta": rnd(delta, 3), "side": side, "direction": direction, "otm": otm,
             "moneyness_pct": rnd((c["strike"] / spot - 1) * 100, 1) if spot else None,
             "hedge_like": hedge_like, "whale": prem >= ocfg["big_premium"],
             "last_trade_time": c.get("last_trade_time")}
        u["score"] = contract_score(u, ocfg)
        unusual.append(u)
    # en valores muy liquidos (SPY, SPX, NVDA...) exige ademas un % minimo de la prima total del dia
    floor = (cprem + pprem) * ocfg.get("min_premium_share", 0.0)
    unusual = [u for u in unusual if u["premium"] >= floor]
    # ballena relativa: >= big_premium y >= 1% de toda la prima del valor
    whale_floor = max(ocfg["big_premium"], 0.01 * (cprem + pprem))
    for u in unusual:
        u["whale"] = u["premium"] >= whale_floor
    # posibles spreads/combinaciones: 2 patas del mismo vencimiento con volumen casi igual
    by_exp = {}
    for u in unusual:
        by_exp.setdefault(u["expiration"], []).append(u)
    for us in by_exp.values():
        for i, a in enumerate(us):
            for b in us[i + 1:]:
                # mismo vencimiento, volumen casi igual y (distinto tipo o distinto lado):
                # straddle/strangle/risk reversal o spread vertical
                ratio = a["volume"] / max(b["volume"], 1)
                vertical = a["kind"] == b["kind"] and {a["side"], b["side"]} == {"ASK", "BID"}
                if (vertical and 0.4 <= ratio <= 2.5) or (
                        0.8 <= ratio <= 1.25 and (a["kind"] != b["kind"] or a["side"] != b["side"])):
                    a["combo"] = b["combo"] = True
    for u in unusual:
        u.setdefault("combo", False)
        if u["combo"]:
            u["_w"] *= 0.25
            u["_f"] *= 0.25
            u["score"] = contract_score(u, ocfg)
    unusual.sort(key=lambda x: (x["score"], x["premium"]), reverse=True)
    eff = 0.0
    for u in unusual:
        w = u.pop("_w")
        u["weight"] = round(u.pop("_f"), 3)
        eff += u["premium"] * u["weight"]
        if u["direction"] == "ALCISTA":
            bull += u["premium"] * w
        elif u["direction"] == "BAJISTA":
            bear += u["premium"] * w
    unusual.sort(key=lambda x: (x["score"], x["premium"]), reverse=True)

    m.update({
        "call_vol": int(cv), "put_vol": int(pv), "call_oi": int(coi), "put_oi": int(poi),
        "opt_vol": int(cv + pv),
        "cp_vol_ratio": safe_ratio(cv, pv), "cp_oi_ratio": safe_ratio(coi, poi),
        "pc_vol_ratio": safe_ratio(pv, cv),
        "vol_oi_total": safe_ratio(cv + pv, coi + poi),
        "call_premium": round(cprem), "put_premium": round(pprem),
        "bull_premium": round(bull), "bear_premium": round(bear),
        "unusual_count": len(unusual),
        "unusual_premium": round(sum(u["premium"] for u in unusual)),
        "effective_premium": round(eff),   # sin coberturas/spreads/semanales: lo que de verdad cuenta
        "whales": sum(1 for u in unusual if u["whale"]),
        "gex_usd_1pct": round(gex) if spot else None,
    })
    tot = bull + bear
    m["flow_bias"] = round((bull - bear) / tot, 3) if tot > 0 else 0.0

    term = atm_iv_by_exp(contracts, spot) if spot else []
    iv30_calc = interp_iv30(term)
    m["iv30"] = rnd(und.get("iv30") or iv30_calc, 4)
    m["iv_front"] = rnd(term[0][2], 4) if term else None
    m["iv_term"] = [{"dte": d, "expiration": e, "iv": rnd(iv, 4)} for d, e, iv in term[:10]]
    if m["iv_front"] and m["iv30"]:
        m["front_premium"] = rnd(m["iv_front"] / m["iv30"], 2)   # >1.2 => evento descontado
    m["expected_moves"] = expected_moves(contracts, spot) if spot else []
    mexp = pick_monthly(contracts, session)
    m["max_pain_exp"] = mexp
    m["max_pain"] = max_pain(contracts, mexp) if mexp else None
    m["call_wall"] = max(call_oi_k, key=call_oi_k.get) if call_oi_k else None
    m["put_wall"] = max(put_oi_k, key=put_oi_k.get) if put_oi_k else None
    return m, unusual


def oi_lookup(contracts):
    return {c["symbol"]: c["oi"] for c in contracts if c.get("symbol")}


# ------------------------------------------------------------------ utilidades v2.2
GEX_TICKERS = {"SPX", "SPY", "QQQ", "NDX", "IWM"}


def option_grid(contracts, spot, min_dte=10, max_dte=200, max_exps=8, width=0.45):
    """Rejilla compacta (vencimiento -> contratos cerca del dinero) para construir ideas con
    strikes y precios REALES. Solo en memoria; no se publica entera."""
    if not spot:
        return {}
    by = {}
    for c in contracts:
        if not (min_dte <= c["dte"] <= max_dte):
            continue
        if abs(c["strike"] / spot - 1) > width:
            continue
        mid = mid_price(c)
        if mid <= 0 or c["ask"] <= 0:
            continue
        by.setdefault(c["expiration"], []).append({
            "symbol": c["symbol"], "kind": c["kind"], "strike": c["strike"], "dte": c["dte"],
            "expiration": c["expiration"], "mid": round(mid, 2), "bid": c["bid"], "ask": c["ask"],
            "oi": c["oi"], "delta": rnd(c.get("delta"), 3), "iv": rnd(c["iv"], 4)})
    exps = sorted(by.keys())[:max_exps]
    return {e: sorted(by[e], key=lambda x: (x["kind"], x["strike"])) for e in exps}


def price_map(contracts, symbols):
    out = {}
    if not symbols:
        return out
    for c in contracts:
        if c["symbol"] in symbols:
            out[c["symbol"]] = {"mid": round(mid_price(c), 3), "bid": c["bid"], "ask": c["ask"],
                                "last": c["last"], "oi": c["oi"]}
    return out


def gex_profile(contracts, spot, pct_range=0.06, step=0.005, max_dte=60):
    """Exposicion gamma de los creadores de mercado a distintos precios del indice.
    Supuesto estandar: los dealers estan largos de calls y cortos de puts de los clientes
    (GEX = gamma_call*OI - gamma_put*OI). GEX > 0 => frenan movimientos; GEX < 0 => los amplifican.
    Devuelve gex actual, nivel de 'flip' (cambio de signo) y muros."""
    if not spot:
        return None
    cs = [c for c in contracts if 0 <= c["dte"] <= max_dte and c["oi"] > 0 and 0.01 < c["iv"] < 5]
    if not cs:
        return None
    levels = []
    n = int(round(pct_range / step))
    for i in range(-n, n + 1):
        s = spot * (1 + i * step)
        tot = 0.0
        for c in cs:
            _, g = bs_greeks(c["kind"], s, c["strike"], max(c["dte"], 0.5) / 365.0, c["iv"])
            if g:
                v = g * c["oi"] * 100 * s * s * 0.01
                tot += v if c["kind"] == "C" else -v
        levels.append((round(s, 2), tot))
    now = min(levels, key=lambda x: abs(x[0] - spot))[1]
    flip = None
    for (s1, g1), (s2, g2) in zip(levels, levels[1:]):
        if g1 == 0 or (g1 < 0) != (g2 < 0):
            x = s1 + (s2 - s1) * (abs(g1) / (abs(g1) + abs(g2))) if (g1 or g2) else s1
            if flip is None or abs(x - spot) < abs(flip - spot):
                flip = round(x, 2)
    call_oi, put_oi = {}, {}
    for c in cs:
        d = call_oi if c["kind"] == "C" else put_oi
        d[c["strike"]] = d.get(c["strike"], 0) + c["oi"]
    return {
        "spot": rnd(spot, 2), "gex_now": round(now), "regime": "positiva" if now >= 0 else "negativa",
        "flip": flip, "call_wall": max(call_oi, key=call_oi.get) if call_oi else None,
        "put_wall": max(put_oi, key=put_oi.get) if put_oi else None,
        "profile": [{"s": s, "g": round(g)} for s, g in levels],
    }


def recompute_flow(m, unusual):
    """Recalcula prima alcista/bajista/efectiva tras re-etiquetar contratos (p.ej. dividendos)."""
    bull = bear = eff = 0.0
    for u in unusual:
        _, w = DIR_WEIGHTS[(u["kind"][0], u["side"])]
        wt = u.get("weight", 1.0)
        eff += u["premium"] * wt
        if u["direction"] == "ALCISTA":
            bull += u["premium"] * w * wt
        elif u["direction"] == "BAJISTA":
            bear += u["premium"] * w * wt
    m["bull_premium"], m["bear_premium"], m["effective_premium"] = round(bull), round(bear), round(eff)
    tot = bull + bear
    m["flow_bias"] = round((bull - bear) / tot, 3) if tot > 0 else 0.0
    return m
