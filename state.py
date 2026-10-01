"""Estado persistente entre ejecuciones (rama `data` del repo):

- state/ticker_daily.csv : agregados diarios por valor (volumen de opciones, IV30...)
  -> volumen relativo y rango/percentil de IV.
- state/flags.csv        : contratos inusuales marcados -> confirmacion por OI al dia siguiente.
- state/enrich.json      : cache de datos de Yahoo (fundamentales, noticias...).
- state/alerts.json      : alertas ya enviadas (para no repetir).
"""
import datetime as dt
import os

import pandas as pd

from .config import state_dir
from .util import parse_date, read_json, write_json

DAILY_COLS = ["date", "ticker", "price", "opt_vol", "call_vol", "put_vol", "iv30",
              "unusual_premium", "bull_premium", "bear_premium", "stock_volume"]
FLAG_COLS = ["date", "ticker", "symbol", "kind", "strike", "expiration", "volume", "oi_before",
             "premium", "direction", "side", "status", "oi_after", "checked_date"]


def _path(name):
    return os.path.join(state_dir(), name)


# ---------------------------------------------------------------- diario
def load_daily():
    p = _path("ticker_daily.csv")
    if os.path.exists(p):
        try:
            df = pd.read_csv(p, dtype={"date": str, "ticker": str})
            for c in DAILY_COLS:
                if c not in df.columns:
                    df[c] = None
            return df[DAILY_COLS]
        except Exception:
            pass
    return pd.DataFrame(columns=DAILY_COLS)


def upsert_daily(df, metrics_list, keep_days=420):
    rows = []
    for m in metrics_list:
        if not m.get("session_date"):
            continue
        rows.append({"date": m["session_date"], "ticker": m["ticker"], "price": m.get("price"),
                     "opt_vol": m.get("opt_vol"), "call_vol": m.get("call_vol"),
                     "put_vol": m.get("put_vol"), "iv30": m.get("iv30"),
                     "unusual_premium": m.get("unusual_premium"),
                     "bull_premium": m.get("bull_premium"), "bear_premium": m.get("bear_premium"),
                     "stock_volume": m.get("stock_volume")})
    if not rows:
        return df
    new = pd.DataFrame(rows, columns=DAILY_COLS)
    keys = set(zip(new["date"], new["ticker"]))
    if len(df):
        mask = [(d, t) not in keys for d, t in zip(df["date"], df["ticker"])]
        df = df[mask]
    out = pd.concat([df, new], ignore_index=True) if len(df) else new
    cutoff = (dt.date.today() - dt.timedelta(days=keep_days)).isoformat()
    out = out[out["date"] >= cutoff]
    return out.sort_values(["ticker", "date"]).reset_index(drop=True)


def save_daily(df):
    os.makedirs(state_dir(), exist_ok=True)
    df.to_csv(_path("ticker_daily.csv"), index=False)


def baselines(df, ticker, session, opt_vol, iv30, lookback=20):
    """Volumen relativo (vs media de las N sesiones previas) e IV rank/percentil (1 ano)."""
    out = {"rel_vol": None, "avg_opt_vol": None, "iv_rank": None, "iv_pct": None, "history_days": 0}
    if df is None or not len(df):
        return out
    h = df[(df["ticker"] == ticker) & (df["date"] < session)].sort_values("date")
    out["history_days"] = int(len(h))
    vols = pd.to_numeric(h["opt_vol"], errors="coerce").dropna().tail(lookback)
    if len(vols) >= 5 and vols.mean() > 0:
        out["avg_opt_vol"] = round(float(vols.mean()))
        if opt_vol is not None:
            out["rel_vol"] = round(float(opt_vol) / float(vols.mean()), 2)
    ivs = pd.to_numeric(h["iv30"], errors="coerce").dropna().tail(252)
    if iv30 is not None and len(ivs) >= 20:
        lo, hi = float(ivs.min()), float(ivs.max())
        allv = list(ivs) + [iv30]
        lo, hi = min(lo, iv30), max(hi, iv30)
        out["iv_rank"] = round((iv30 - lo) / (hi - lo) * 100, 1) if hi > lo else 50.0
        out["iv_pct"] = round(sum(1 for v in allv if v <= iv30) / len(allv) * 100, 1)
    return out


# ---------------------------------------------------------------- flags / confirmacion OI
def load_flags():
    p = _path("flags.csv")
    if os.path.exists(p):
        try:
            df = pd.read_csv(p, dtype={"date": str, "symbol": str, "status": str, "checked_date": str})
            for c in FLAG_COLS:
                if c not in df.columns:
                    df[c] = None
            df["status"] = df["status"].fillna("PENDIENTE")
            return df[FLAG_COLS]
        except Exception:
            pass
    return pd.DataFrame(columns=FLAG_COLS)


def add_flags(flags, unusual, session):
    """Guarda los inusuales de la sesion (upsert por fecha+contrato; el volumen intradia crece)."""
    if not unusual:
        return flags
    rows = [{"date": session, "ticker": u["ticker"], "symbol": u["symbol"], "kind": u["kind"],
             "strike": u["strike"], "expiration": u["expiration"], "volume": u["volume"],
             "oi_before": u["oi"], "premium": u["premium"], "direction": u["direction"],
             "side": u["side"], "status": "PENDIENTE", "oi_after": None, "checked_date": None}
            for u in unusual if u.get("symbol")]
    new = pd.DataFrame(rows, columns=FLAG_COLS)
    if len(flags):
        keys = set(zip(new["date"], new["symbol"]))
        flags = flags[[(d, s) not in keys for d, s in zip(flags["date"], flags["symbol"])]]
        return pd.concat([flags, new], ignore_index=True)
    return new


def confirm_flags(flags, ticker, oi_map, session, ratio=0.5):
    """Evalua (una sola vez) los flags de sesiones anteriores con el OI actual.
    El OI que publica Cboe es el del cierre previo (OCC lo actualiza de madrugada):
    si el OI sube >= ratio * volumen marcado, las posiciones eran de APERTURA."""
    if not len(flags):
        return flags
    sel = (flags["ticker"] == ticker) & (flags["date"] < session) & (flags["status"] == "PENDIENTE")
    if not sel.any():
        return flags
    flags = flags.copy()
    for idx in flags.index[sel]:
        sym = flags.at[idx, "symbol"]
        exp = parse_date(flags.at[idx, "expiration"])
        if sym in oi_map:
            oi_now = float(oi_map[sym])
            oi_before = float(flags.at[idx, "oi_before"] or 0)
            vol = float(flags.at[idx, "volume"] or 0)
            flags.at[idx, "oi_after"] = oi_now
            flags.at[idx, "status"] = "CONFIRMADA" if oi_now - oi_before >= ratio * vol else "NO CONFIRMADA"
            flags.at[idx, "checked_date"] = session
        elif exp and exp.isoformat() < session:
            flags.at[idx, "status"] = "VENCIDA"
            flags.at[idx, "checked_date"] = session
    return flags


def flag_summary(flags, ticker, session, window_sessions=5):
    out = {"confirmed": 0, "confirmed_bull_premium": 0, "confirmed_bear_premium": 0,
           "not_confirmed": 0, "pending": 0, "confirmed_list": [],
           "prev_bull_premium": 0, "prev_bear_premium": 0}
    if not len(flags):
        return out
    d0 = parse_date(session)
    since = (d0 - dt.timedelta(days=int(window_sessions * 7 / 5) + 2)).isoformat() if d0 else "0000"
    f = flags[(flags["ticker"] == ticker) & (flags["date"] >= since)]
    for _, r in f.iterrows():
        st = r["status"]
        if r["date"] < session and st in ("PENDIENTE", "CONFIRMADA"):
            k = "prev_bull_premium" if r["direction"] == "ALCISTA" else "prev_bear_premium"
            out[k] += float(r["premium"] or 0)
        if st == "CONFIRMADA":
            out["confirmed"] += 1
            prem = float(r["premium"] or 0)
            if r["direction"] == "ALCISTA":
                out["confirmed_bull_premium"] += prem
            else:
                out["confirmed_bear_premium"] += prem
            out["confirmed_list"].append({
                "date": r["date"], "symbol": r["symbol"], "kind": r["kind"], "strike": r["strike"],
                "expiration": r["expiration"], "volume": int(float(r["volume"] or 0)),
                "oi_before": int(float(r["oi_before"] or 0)),
                "oi_after": int(float(r["oi_after"] or 0)), "premium": int(prem),
                "direction": r["direction"]})
        elif st == "NO CONFIRMADA":
            out["not_confirmed"] += 1
        elif st == "PENDIENTE":
            out["pending"] += 1
    out["confirmed_bull_premium"] = round(out["confirmed_bull_premium"])
    out["confirmed_bear_premium"] = round(out["confirmed_bear_premium"])
    out["prev_bull_premium"] = round(out["prev_bull_premium"])
    out["prev_bear_premium"] = round(out["prev_bear_premium"])
    out["confirmed_list"] = sorted(out["confirmed_list"], key=lambda x: -x["premium"])[:8]
    return out


def prune_flags(flags, keep_days=45):
    if not len(flags):
        return flags
    cutoff = (dt.date.today() - dt.timedelta(days=keep_days)).isoformat()
    return flags[flags["date"] >= cutoff].reset_index(drop=True)


def save_flags(flags):
    os.makedirs(state_dir(), exist_ok=True)
    flags.to_csv(_path("flags.csv"), index=False)


# ---------------------------------------------------------------- caches JSON
def load_cache(name, default=None):
    return read_json(_path(name), default if default is not None else {})


def save_cache(name, obj):
    write_json(_path(name), obj)
