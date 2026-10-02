"""Registro de aciertos: guarda cada senal y mide a 5, 10 y 20 sesiones si el precio fue
en la direccion indicada. Asi se sabe CON NUMEROS que tipo de senal funciona.

state/signals.csv  -> una fila por senal (sin repetir el mismo valor+direccion en 5 sesiones)
tracking.json      -> estadisticas por grupo y ultimas senales con su resultado
"""
import datetime as dt
import os

import pandas as pd

from .config import state_dir
from .util import iso_now, parse_date

HORIZONS = (5, 10, 20)
SIG_COLS = ["date", "ticker", "direction", "score", "signal", "entrada", "flujo_confirmado", "pre_alerta",
            "catalyst", "sector", "price", "squeeze"]


def _path():
    return os.path.join(state_dir(), "signals.csv")


def load_signals():
    p = _path()
    if os.path.exists(p):
        try:
            df = pd.read_csv(p, dtype={"date": str, "ticker": str})
            for c in SIG_COLS:
                if c not in df.columns:
                    df[c] = None
            return df[SIG_COLS]
        except Exception:
            pass
    return pd.DataFrame(columns=SIG_COLS)


def save_signals(df):
    os.makedirs(state_dir(), exist_ok=True)
    df.to_csv(_path(), index=False)


def qualifies(r):
    ck = r.get("checklist") or {}
    return (r.get("direction") in ("ALCISTA", "BAJISTA") and r.get("price")
            and (r.get("signal") in ("ALTA", "MEDIA") or ck.get("entrada") or ck.get("pre_alerta")))


def record_signals(df, records, session, dedupe_days=7):
    """Anade las senales de la sesion (la ultima version del dia sustituye a las anteriores)."""
    d0 = parse_date(session)
    if not d0:
        return df
    since = (d0 - dt.timedelta(days=dedupe_days)).isoformat()
    rows = []
    for r in records:
        if not qualifies(r):
            continue
        prev = df[(df["ticker"] == r["ticker"]) & (df["direction"] == r["direction"]) &
                  (df["date"] >= since) & (df["date"] < session)] if len(df) else df
        if len(prev):
            continue   # ya registrada hace pocos dias: no duplicar
        ck = r.get("checklist") or {}
        nc = r.get("next_catalyst") or {}
        rows.append({"date": session, "ticker": r["ticker"], "direction": r["direction"],
                     "score": r.get("score"), "signal": r.get("signal"), "entrada": bool(ck.get("entrada")),
                     "flujo_confirmado": bool(ck.get("flujo_confirmado")), "pre_alerta": bool(ck.get("pre_alerta")),
                     "catalyst": nc.get("type") or "", "sector": r.get("sector") or "", "price": r.get("price"),
                     "squeeze": (r.get("squeeze") or {}).get("level") or ""})
    if not rows:
        return df
    new = pd.DataFrame(rows, columns=SIG_COLS)
    if len(df):
        keys = set(zip(new["date"], new["ticker"], new["direction"]))
        df = df[[(d, t, x) not in keys for d, t, x in zip(df["date"], df["ticker"], df["direction"])]]
        return pd.concat([df, new], ignore_index=True)
    return new


def evaluate(signals, daily):
    """Devuelve lista de senales con r5/r10/r20 (rentabilidad a favor de la direccion, %)."""
    if not len(signals):
        return []
    px = {}
    if daily is not None and len(daily):
        for tk, g in daily.dropna(subset=["price"]).groupby("ticker"):
            px[tk] = sorted(zip(g["date"], pd.to_numeric(g["price"], errors="coerce")))
    out = []
    for _, s in signals.iterrows():
        series = [(d, p) for d, p in px.get(s["ticker"], []) if d > s["date"] and p == p]
        sign = 1 if s["direction"] == "ALCISTA" else -1
        p0 = float(s["price"]) if s["price"] == s["price"] and s["price"] else None
        row = {k: (None if (isinstance(v, float) and v != v) else v) for k, v in s.to_dict().items()}
        for h in HORIZONS:
            if p0 and len(series) >= h:
                ret = (series[h - 1][1] / p0 - 1) * 100 * sign
                row[f"r{h}"] = round(ret, 2)
            else:
                row[f"r{h}"] = None
        row["sessions_since"] = len(series)
        row["last_ret"] = round((series[-1][1] / p0 - 1) * 100 * sign, 2) if p0 and series else None
        out.append(row)
    return out


def _stats(rows):
    out = {"n": len(rows)}
    for h in HORIZONS:
        vals = [r[f"r{h}"] for r in rows if r.get(f"r{h}") is not None]
        out[f"n{h}"] = len(vals)
        out[f"hit{h}"] = round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1) if vals else None
        out[f"avg{h}"] = round(sum(vals) / len(vals), 2) if vals else None
    return out


def summarize(evals):
    groups = {
        "Todas": lambda r: True,
        "ENTRADA": lambda r: bool(r.get("entrada")),
        "Flujo confirmado por OI": lambda r: bool(r.get("flujo_confirmado")),
        "Pre-alerta": lambda r: bool(r.get("pre_alerta")),
        "Score ALTA": lambda r: r.get("signal") == "ALTA",
        "Score MEDIA": lambda r: r.get("signal") == "MEDIA",
        "Alcistas": lambda r: r.get("direction") == "ALCISTA",
        "Bajistas": lambda r: r.get("direction") == "BAJISTA",
        "Con catalizador FDA": lambda r: any(k in (r.get("catalyst") or "") for k in ("PDUFA", "AdCom", "Lectura", "Ensayo")),
        "Antes de resultados": lambda r: r.get("catalyst") == "Resultados",
        "Riesgo de apreton de cortos": lambda r: bool(r.get("squeeze")),
    }
    return [{"group": g, **_stats([r for r in evals if f(r)])} for g, f in groups.items()]


def build(signals, daily):
    ev = evaluate(signals, daily)
    ev.sort(key=lambda r: (r["date"], r.get("score") or 0), reverse=True)
    return {"generated_at": iso_now(), "horizons": list(HORIZONS), "stats": summarize(ev),
            "signals": ev[:400], "total": len(ev),
            "note": "Rentabilidad de la ACCION en la direccion de la senal (no de la opcion). "
                    "Necesita 5/10/20 sesiones para tener datos."}
