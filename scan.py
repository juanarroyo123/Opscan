#!/usr/bin/env python3
"""
OpScan - escaner de opciones estilo "volumen inusual" sobre datos de Yahoo Finance.

Lee config/watchlist.json y config/catalysts.csv, descarga las cadenas de opciones
y fundamentales con yfinance, calcula ratios call/put, detecta volumen inusual,
estima IV ATM y percentil de IV (con histórico propio), puntúa cada valor y
escribe docs/data/latest.json para que lo muestre la web.

Pensado para ejecutarse en GitHub Actions (donde Yahoo es accesible).
"""
import json
import os
import csv
import math
from datetime import datetime, timezone, date

import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = ROOT
DATA_DIR = ROOT
HISTORY_CSV = os.path.join(DATA_DIR, "history.csv")
LATEST_JSON = os.path.join(DATA_DIR, "latest.json")

DEFAULTS = {
    "tickers": ["NVDA", "AAPL", "TSLA"],
    "max_expirations": 3,        # cuantos vencimientos cercanos agregar
    "max_days_out": 70,          # ignorar vencimientos mas alla de N dias
    "unusual_min_volume": 250,   # volumen minimo de un contrato para marcarlo
    "unusual_vol_oi_mult": 1.0,  # volumen >= mult * OI  => inusual (abriendo posiciones)
    "catalyst_horizon_days": 45, # ventana para considerar un catalizador "proximo"
    "signal_min_gain_pct": 5,    # potencial minimo (%) para que cuente como "señal" de compra
}


# --------------------------------------------------------------------------
# Configuracion
# --------------------------------------------------------------------------
def load_config():
    cfg = dict(DEFAULTS)
    path = os.path.join(CONFIG_DIR, "watchlist.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        cfg.update({k: v for k, v in user.items() if v is not None})
    return cfg


def load_catalysts():
    """Catalizadores manuales (PDUFA, ensayos, etc.) que Yahoo no da."""
    path = os.path.join(CONFIG_DIR, "catalysts.csv")
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            tk = (row.get("ticker") or "").strip().upper()
            d = (row.get("date") or "").strip()
            if not tk or not d:
                continue
            out.setdefault(tk, []).append({
                "date": d,
                "type": (row.get("type") or "").strip(),
                "note": (row.get("note") or "").strip(),
            })
    return out


# --------------------------------------------------------------------------
# Funciones de metricas (puras, testeables sin red)
# --------------------------------------------------------------------------
def _num(x):
    try:
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return 0.0
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def aggregate_chain(calls, puts):
    cv = float(calls["volume"].fillna(0).sum()) if len(calls) else 0.0
    pv = float(puts["volume"].fillna(0).sum()) if len(puts) else 0.0
    coi = float(calls["openInterest"].fillna(0).sum()) if len(calls) else 0.0
    poi = float(puts["openInterest"].fillna(0).sum()) if len(puts) else 0.0
    return cv, pv, coi, poi


def ratio(a, b):
    if b <= 0:
        return None
    return round(a / b, 3)


def atm_iv(calls, puts, price):
    """IV media de la call y put mas cercanas al dinero."""
    def nearest_iv(df):
        if price is None or df is None or len(df) == 0:
            return None
        idx = (df["strike"] - price).abs().idxmin()
        iv = df.loc[idx, "impliedVolatility"]
        return float(iv) if pd.notna(iv) else None
    vals = [v for v in (nearest_iv(calls), nearest_iv(puts)) if v]
    return round(sum(vals) / len(vals), 4) if vals else None


def find_unusual(df, kind, min_vol, vol_oi_mult, top=6):
    """Contratos con volumen alto Y volumen >= mult*OI (posiciones abriendose)."""
    rows = []
    if df is None or len(df) == 0:
        return rows
    for _, r in df.iterrows():
        v = _num(r.get("volume"))
        oi = _num(r.get("openInterest"))
        if v >= min_vol and v >= vol_oi_mult * max(oi, 1):
            rows.append({
                "kind": kind,
                "strike": _num(r.get("strike")),
                "expiration": r.get("expiration", ""),
                "volume": int(v),
                "openInterest": int(oi),
                "vol_oi": round(v / max(oi, 1), 2),
                "iv": round(_num(r.get("impliedVolatility")), 4),
                "lastPrice": round(_num(r.get("lastPrice")), 2),
            })
    rows.sort(key=lambda x: x["volume"], reverse=True)
    return rows[:top]


def days_until(date_str):
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            d = datetime.strptime(str(date_str), fmt).date()
            return (d - date.today()).days
        except ValueError:
            continue
    return None


def score_candidate(rec, cfg):
    """Puntuacion 0-100 transparente, suma de senales. Replica el checklist del manual."""
    pts = 0.0
    reasons = []

    # 1) Catalizador proximo (0-30)
    d = rec.get("next_catalyst_days")
    if d is not None and d >= 0:
        h = cfg["catalyst_horizon_days"]
        if d <= h:
            v = 30 * (1 - d / h)
            pts += v
            reasons.append(f"Catalizador en {d}d (+{v:.0f})")

    # 2) Ratio call/put en extremo (0-25)
    cpv = rec.get("cp_vol_ratio")
    if cpv is not None:
        if cpv <= 0.4 or cpv >= 2.5:
            pts += 25
            reasons.append("Ratio C/P extremo (+25)")
        elif cpv <= 0.6 or cpv >= 1.8:
            pts += 15
            reasons.append("Ratio C/P marcado (+15)")

    # 3) Volumen inusual (0-25)
    n = rec.get("unusual_count", 0)
    if n >= 3:
        pts += 25
        reasons.append(f"{n} contratos inusuales (+25)")
    elif n >= 1:
        pts += 12
        reasons.append(f"{n} contrato(s) inusual(es) (+12)")

    # 4) Volumen total / OI total (0-10)
    vo = rec.get("total_vol_oi")
    if vo is not None and vo >= 0.5:
        pts += 10
        reasons.append("Vol/OI agregado alto (+10)")
    elif vo is not None and vo >= 0.25:
        pts += 5
        reasons.append("Vol/OI agregado medio (+5)")

    # 5) Percentil de IV (0-10) -- solo si hay historico
    ivp = rec.get("iv_percentile")
    if ivp is not None:
        if ivp >= 90:
            pts += 10
            reasons.append("IV en percentil 90+ (+10)")
        elif ivp >= 70:
            pts += 5
            reasons.append("IV en percentil 70+ (+5)")

    pts = round(min(pts, 100), 1)
    label = "ALTA" if pts >= 60 else ("MEDIA" if pts >= 35 else "BAJA")
    return pts, label, reasons


# --------------------------------------------------------------------------
# Historico de IV (para percentiles)
# --------------------------------------------------------------------------
def load_history():
    if not os.path.exists(HISTORY_CSV):
        return pd.DataFrame(columns=["date", "ticker", "atm_iv"])
    try:
        return pd.read_csv(HISTORY_CSV)
    except Exception:
        return pd.DataFrame(columns=["date", "ticker", "atm_iv"])


def iv_percentile(hist, ticker, current_iv):
    if current_iv is None:
        return None
    s = hist[hist["ticker"] == ticker]["atm_iv"].dropna()
    if len(s) < 8:        # necesita historia para ser fiable
        return None
    pct = (s <= current_iv).mean() * 100
    return round(float(pct), 1)


def append_history(records):
    today = date.today().isoformat()
    rows = []
    for r in records:
        if r.get("atm_iv") is not None:
            rows.append({"date": today, "ticker": r["ticker"], "atm_iv": r["atm_iv"]})
    if not rows:
        return
    hist = load_history()
    drop = [x["ticker"] for x in rows]
    hist = hist[~((hist["date"] == today) & (hist["ticker"].isin(drop)))]
    hist = pd.concat([hist, pd.DataFrame(rows)], ignore_index=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    hist.to_csv(HISTORY_CSV, index=False)


# --------------------------------------------------------------------------
# Procesado por ticker (necesita red / yfinance)
# --------------------------------------------------------------------------
def process_ticker(ticker, cfg, catalysts, hist):
    import yfinance as yf
    rec = {"ticker": ticker, "error": None}
    try:
        t = yf.Ticker(ticker)

        # precio
        price = None
        try:
            price = float(t.fast_info["last_price"])
        except Exception:
            h = t.history(period="1d")
            if len(h):
                price = float(h["Close"].iloc[-1])
        rec["price"] = round(price, 2) if price else None

        # fundamentales (best effort)
        try:
            info = t.info or {}
        except Exception:
            info = {}
        rec["name"] = info.get("shortName") or ticker
        rec["market_cap"] = info.get("marketCap")
        rec["cash"] = info.get("totalCash")
        rec["debt"] = info.get("totalDebt")
        rec["inst_pct"] = info.get("heldPercentInstitutions")
        rec["short_pct"] = info.get("shortPercentOfFloat")
        rec["target_mean"] = info.get("targetMeanPrice")
        rec["target_low"] = info.get("targetLowPrice")
        rec["target_high"] = info.get("targetHighPrice")
        rec["recommendation_key"] = info.get("recommendationKey")   # strongBuy/buy/hold/sell/strongSell
        rec["analyst_count"] = info.get("numberOfAnalystOpinions")
        if rec["target_mean"] and price:
            rec["upside_pct"] = round((rec["target_mean"] / price - 1) * 100, 1)

        # señal de compra/venta estilo "stock signals": entrada ~ precio actual,
        # objetivo de venta = precio medio objetivo de los analistas.
        rec["buy_target"] = rec["price"]
        rec["sell_target"] = round(rec["target_mean"], 2) if rec["target_mean"] else None
        rec["potential_gain_pct"] = rec.get("upside_pct")
        rec["is_signal"] = bool(
            rec["sell_target"] and rec["potential_gain_pct"] is not None
            and rec["potential_gain_pct"] >= cfg["signal_min_gain_pct"]
        )

        # desglose de recomendaciones de analistas (para el gauge Strong Sell..Strong Buy)
        rec["analyst_breakdown"] = None
        try:
            rt = t.recommendations
            if rt is not None and len(rt):
                row = rt[rt["period"] == "0m"]
                if len(row):
                    row = row.iloc[0]
                    rec["analyst_breakdown"] = {
                        "strongBuy": int(row.get("strongBuy") or 0),
                        "buy": int(row.get("buy") or 0),
                        "hold": int(row.get("hold") or 0),
                        "sell": int(row.get("sell") or 0),
                        "strongSell": int(row.get("strongSell") or 0),
                    }
        except Exception:
            pass

        # noticias recientes (best effort, el esquema de yfinance ha cambiado con el tiempo)
        rec["news"] = []
        try:
            for n in (t.news or [])[:6]:
                c = n.get("content") if isinstance(n.get("content"), dict) else n
                title = c.get("title")
                link = (
                    (c.get("canonicalUrl") or {}).get("url")
                    or (c.get("clickThroughUrl") or {}).get("url")
                    or c.get("link")
                )
                publisher = (c.get("provider") or {}).get("displayName") or c.get("publisher")
                pub_time = c.get("pubDate") or n.get("providerPublishTime")
                if title and link:
                    rec["news"].append({
                        "title": title[:140],
                        "link": link,
                        "publisher": publisher or "",
                        "pub_date": str(pub_time) if pub_time else None,
                    })
        except Exception:
            pass

        # vencimientos cercanos
        exps = list(t.options or [])
        chosen = []
        for e in exps:
            d = days_until(e)
            if d is not None and 0 <= d <= cfg["max_days_out"]:
                chosen.append(e)
            if len(chosen) >= cfg["max_expirations"]:
                break
        if not chosen and exps:
            chosen = exps[: cfg["max_expirations"]]
        rec["expirations"] = chosen

        all_calls, all_puts = [], []
        for e in chosen:
            try:
                oc = t.option_chain(e)
            except Exception:
                continue
            c = oc.calls.copy()
            p = oc.puts.copy()
            c["expiration"] = e
            p["expiration"] = e
            all_calls.append(c)
            all_puts.append(p)

        if all_calls:
            calls = pd.concat(all_calls, ignore_index=True)
            puts = pd.concat(all_puts, ignore_index=True)
            cv, pv, coi, poi = aggregate_chain(calls, puts)
            rec["call_vol"], rec["put_vol"] = int(cv), int(pv)
            rec["call_oi"], rec["put_oi"] = int(coi), int(poi)
            rec["cp_vol_ratio"] = ratio(cv, pv)
            rec["cp_oi_ratio"] = ratio(coi, poi)
            rec["total_vol_oi"] = ratio(cv + pv, coi + poi)
            rec["atm_iv"] = atm_iv(calls[calls["expiration"] == chosen[0]],
                                   puts[puts["expiration"] == chosen[0]], price)
            unusual = (find_unusual(calls, "CALL", cfg["unusual_min_volume"], cfg["unusual_vol_oi_mult"]) +
                       find_unusual(puts, "PUT", cfg["unusual_min_volume"], cfg["unusual_vol_oi_mult"]))
            unusual.sort(key=lambda x: x["volume"], reverse=True)
            rec["unusual"] = unusual[:8]
            rec["unusual_count"] = len(unusual)
            rec["iv_percentile"] = iv_percentile(hist, ticker, rec["atm_iv"])
        else:
            rec["unusual"] = []
            rec["unusual_count"] = 0

        # catalizadores: earnings (Yahoo) + manuales
        cats = list(catalysts.get(ticker, []))
        try:
            cal = t.calendar
            ed = None
            if isinstance(cal, dict):
                vals = cal.get("Earnings Date") or []
                if vals:
                    ed = vals[0]
            if ed is not None:
                cats.append({"date": str(ed)[:10], "type": "Earnings", "note": "Yahoo"})
        except Exception:
            pass

        norm = []
        for c in cats:
            d = days_until(c["date"])
            if d is not None and d >= -2:
                norm.append({**c, "days": d})
        norm.sort(key=lambda x: x["days"])
        rec["catalysts"] = norm
        rec["next_catalyst_days"] = norm[0]["days"] if norm else None
        rec["next_catalyst"] = (f'{norm[0]["type"]} {norm[0]["date"]}' if norm else None)

        pts, label, reasons = score_candidate(rec, cfg)
        rec["score"] = pts
        rec["signal"] = label
        rec["reasons"] = reasons

    except Exception as e:  # noqa
        rec["error"] = str(e)
        rec["score"] = 0
        rec["signal"] = "ERR"
        rec["unusual"] = []
    return rec


def main():
    cfg = load_config()
    catalysts = load_catalysts()
    hist = load_history()
    tickers = cfg["tickers"]
    print(f"Escaneando {len(tickers)} valores...")

    records = []
    for tk in tickers:
        tk = tk.strip().upper()
        print(f"  - {tk}")
        records.append(process_ticker(tk, cfg, catalysts, hist))

    append_history(records)

    records.sort(key=lambda r: (r.get("score") or 0), reverse=True)
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config": {k: cfg[k] for k in ("max_expirations", "max_days_out",
                                       "unusual_min_volume", "catalyst_horizon_days",
                                       "signal_min_gain_pct")},
        "count": len(records),
        "records": records,
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LATEST_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"Escrito {LATEST_JSON} ({len(records)} valores)")


if __name__ == "__main__":
    main()
