"""Generadores de datos sinteticos realistas para tests sin red."""
import datetime as dt
import math

from opscan.options import bs_greeks


def bs_price(kind, s, k, t, iv, r=0.04):
    if t <= 0:
        return max(0.0, (s - k) if kind == "C" else (k - s))
    sq = iv * math.sqrt(t)
    d1 = (math.log(s / k) + (r + 0.5 * iv * iv) * t) / sq
    d2 = d1 - sq
    n = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
    if kind == "C":
        return s * n(d1) - k * math.exp(-r * t) * n(d2)
    return k * math.exp(-r * t) * n(-d2) - s * n(-d1)


def occ(root, exp, kind, strike):
    return f"{root}{exp.strftime('%y%m%d')}{kind}{int(round(strike * 1000)):08d}"


def cboe_payload(ticker, spot, session, iv=0.35, n_exp=8, unusual=None, oi_override=None,
                 iv30=None, base_vol=40, base_oi=800):
    """Cadena completa con N vencimientos semanales/mensuales.
    unusual: lista de dicts {kind, strike, exp_idx, volume, oi, side} para inyectar flujo."""
    root = ticker.lstrip("_")
    exps = [session + dt.timedelta(days=d) for d in (2, 9, 16, 23, 30, 44, 72, 107, 170, 380)[:n_exp]]
    step = 1 if spot < 50 else 5 if spot < 500 else 25
    strikes = [round(spot / step) * step + i * step for i in range(-10, 11)]
    opts = []
    for e in exps:
        t = (e - session).days / 365
        for k in strikes:
            if k <= 0:
                continue
            for kind in ("C", "P"):
                p = bs_price(kind, spot, k, t, iv)
                d, g = bs_greeks(kind, spot, k, t, iv)
                spread = max(0.02, p * 0.04)
                opts.append({"option": occ(root, e, kind, k), "bid": round(max(p - spread / 2, 0), 2),
                             "ask": round(p + spread / 2, 2), "iv": iv, "open_interest": float(base_oi),
                             "volume": float(base_vol), "delta": d, "gamma": g, "last_trade_price": round(p, 2),
                             "last_trade_time": f"{session}T15:30:00"})
    idx = {o["option"]: o for o in opts}
    for u in unusual or []:
        sym = occ(root, exps[u["exp_idx"]], u["kind"], u["strike"])
        o = idx[sym]
        o["volume"] = float(u["volume"])
        o["open_interest"] = float(u["oi"])
        o["last_trade_price"] = o["ask"] if u.get("side", "ASK") == "ASK" else o["bid"]
    for sym, oi in (oi_override or {}).items():
        if sym in idx:
            idx[sym]["open_interest"] = float(oi)
    return {"timestamp": f"{session} 16:15:00",
            "data": {"symbol": ticker, "current_price": spot, "close": spot, "prev_day_close": spot * 0.99,
                     "price_change_percent": 1.0, "volume": 5_000_000, "iv30": (iv30 or iv) * 100,
                     "iv30_change": 0.5, "last_trade_time": f"{session}T15:59:59", "options": opts}}


def cot_records(code, weeks=160, end=None, trend=1):
    end = end or dt.date(2026, 9, 22)
    out = []
    oi = 1_000_000
    for i in range(weeks):
        d = end - dt.timedelta(weeks=weeks - 1 - i)
        spec_long = 200_000 + trend * i * 600
        oi_chg = 5000 if i % 3 else -3000
        oi += oi_chg
        out.append({"report_date_as_yyyy_mm_dd": d.isoformat() + "T00:00:00.000",
                    "cftc_contract_market_code": code, "market_and_exchange_names": code,
                    "open_interest_all": str(oi), "noncomm_positions_long_all": str(spec_long),
                    "noncomm_positions_short_all": "250000", "comm_positions_long_all": str(900_000 - i * 300),
                    "comm_positions_short_all": "850000", "nonrept_positions_long_all": "100000",
                    "nonrept_positions_short_all": "90000", "change_in_open_interest_all": str(oi_chg),
                    "lev_money_positions_long": str(100_000 + i * 200), "lev_money_positions_short": "300000",
                    "asset_mgr_positions_long": "900000", "asset_mgr_positions_short": str(150_000 + i * 100)})
    return out
