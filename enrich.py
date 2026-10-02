"""Enriquecimiento con Yahoo (yfinance) de los mejores candidatos:
nombre, sector, capitalizacion, short interest, consenso de analistas, proximos resultados,
noticias e insiders corporativos (Formulario 4). Con cache de ~20 h en state/enrich.json.
"""
import datetime as dt
import time

from .util import log, now_utc, num, parse_date, rnd


def _insiders(t):
    try:
        df = t.insider_transactions
    except Exception:
        return None
    if df is None or not len(df):
        return None
    since = dt.date.today() - dt.timedelta(days=90)
    buys = sells = 0
    buy_v = sell_v = 0.0
    rows = []
    for _, r in df.iterrows():
        d = parse_date(r.get("Start Date"))
        if not d or d < since:
            continue
        txt = f"{r.get('Text') or ''} {r.get('Transaction') or ''}".lower()
        val = num(r.get("Value"))
        if "purchase" in txt or "buy" in txt:
            buys += 1; buy_v += val
        elif "sale" in txt or "sell" in txt:
            sells += 1; sell_v += val
        else:
            continue
        if len(rows) < 6:
            rows.append({"date": d.isoformat(), "insider": r.get("Insider"), "position": r.get("Position"),
                         "text": (r.get("Text") or "")[:80], "value": round(val) if val else None})
    return {"buys": buys, "sells": sells, "buy_usd": round(buy_v), "sell_usd": round(sell_v), "recent": rows}


def _epoch_date(v):
    try:
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return dt.datetime.fromtimestamp(float(v), dt.timezone.utc).date().isoformat()
        d = parse_date(v)
        return d.isoformat() if d else None
    except Exception:
        return None


def earnings_reactions(dates_df, hist_close, max_items=8):
    """Movimiento real de la accion en cada resultado pasado.
    dates_df: index = timestamps de resultados (con hora); hist_close: Serie de cierres diarios.
    Antes de apertura (hora < 12 ET): cierre previo -> cierre del dia.
    Tras el cierre (hora >= 12 ET): cierre del dia -> cierre del dia siguiente."""
    import pandas as pd
    if dates_df is None or hist_close is None or not len(hist_close):
        return []
    closes = hist_close.dropna()
    idx = [d.date() if hasattr(d, "date") else d for d in closes.index]
    out = []
    now = dt.date.today()
    for ts in dates_df.index:
        try:
            t = pd.Timestamp(ts)
            if t.tzinfo is not None:
                t = t.tz_convert("America/New_York")
            d = t.date()
        except Exception:
            continue
        if d >= now:
            continue
        amc = t.hour >= 12
        try:
            pos = next(i for i, x in enumerate(idx) if x >= d)
        except StopIteration:
            continue
        if amc:
            if idx[pos] != d or pos + 1 >= len(idx):
                continue
            a, b = closes.iloc[pos], closes.iloc[pos + 1]
        else:
            if pos == 0:
                continue
            a, b = closes.iloc[pos - 1], closes.iloc[pos]
        if a and b:
            out.append({"date": d.isoformat(), "when": "tras cierre" if amc else "antes de apertura",
                        "move_pct": round((float(b) / float(a) - 1) * 100, 2)})
        if len(out) >= max_items:
            break
    return out


def fetch_earnings_history(ticker):
    import yfinance as yf
    from .options import yf_symbol
    t = yf.Ticker(yf_symbol(ticker))
    ed = t.get_earnings_dates(limit=16)
    h = t.history(period="4y", interval="1d", auto_adjust=False)
    moves = earnings_reactions(ed, h["Close"] if h is not None and len(h) else None)
    absm = sorted(abs(m["move_pct"]) for m in moves)
    return {"fetched_at": now_utc().isoformat(timespec="seconds"), "moves": moves,
            "avg_abs": round(sum(absm) / len(absm), 2) if absm else None,
            "median_abs": absm[len(absm) // 2] if absm else None,
            "max_abs": absm[-1] if absm else None}


def earnings_vs_expected(hist, expected_moves, earnings_date):
    """Compara el movimiento que descuentan las opciones para el vencimiento justo despues
    de los resultados con el movimiento medio historico."""
    if not hist or not hist.get("avg_abs") or not earnings_date:
        return None
    ems = sorted(expected_moves or [], key=lambda x: x["dte"])
    em = next((x for x in ems if x["expiration"] >= earnings_date), None)
    if not em:
        return None
    before = [x for x in ems if x["expiration"] < earnings_date and x["dte"] > 0]
    # Movimiento del EVENTO: a la varianza total hasta el vencimiento se le resta la varianza
    # "normal" de esos dias (estimada con el vencimiento anterior a los resultados).
    event = None
    if before:
        b = before[-1]
        base_var_day = (b["em_pct"] ** 2) / b["dte"]
        ev2 = em["em_pct"] ** 2 - base_var_day * max(em["dte"] - 1, 0)
        if ev2 > 0:
            event = round(ev2 ** 0.5, 2)
    implied = event if event is not None else em["em_pct"]
    ratio = round(implied / hist["avg_abs"], 2) if hist["avg_abs"] else None
    label = None
    if ratio is not None:
        label = "caras" if ratio >= 1.25 else ("baratas" if ratio <= 0.8 else "en linea")
    return {"expiration": em["expiration"], "implied_pct": implied, "total_to_exp_pct": em["em_pct"],
            "event_only": event is not None, "hist_avg_pct": hist["avg_abs"],
            "hist_median_pct": hist["median_abs"], "ratio": ratio, "label": label,
            "note": ("Movimiento del dia de resultados extraido de la estructura de vencimientos."
                     if event is not None else
                     "Sin vencimiento previo: el movimiento incluye todo hasta el vencimiento (sobreestima).")}


def fetch_one(ticker, n_news=5):
    import yfinance as yf
    from .options import yf_symbol
    t = yf.Ticker(yf_symbol(ticker))
    out = {"fetched_at": now_utc().isoformat(timespec="seconds")}
    try:
        info = t.info or {}
    except Exception:
        info = {}
    out.update({
        "name": info.get("shortName") or info.get("longName"),
        "sector": info.get("sector"), "industry": info.get("industry"),
        "market_cap": info.get("marketCap"), "short_pct_float": info.get("shortPercentOfFloat"),
        "inst_pct": info.get("heldPercentInstitutions"), "insider_pct": info.get("heldPercentInsiders"),
        "target_mean": info.get("targetMeanPrice"), "target_low": info.get("targetLowPrice"),
        "target_high": info.get("targetHighPrice"), "recommendation": info.get("recommendationKey"),
        "analysts": info.get("numberOfAnalystOpinions"), "beta": info.get("beta"),
        "cash": info.get("totalCash"), "debt": info.get("totalDebt"),
        "short_ratio": info.get("shortRatio"),            # dias para cubrir
        "dividend_rate": info.get("dividendRate"), "dividend_yield": info.get("dividendYield"),
        "ex_div_date": _epoch_date(info.get("exDividendDate")),
    })
    try:
        rt = t.recommendations
        if rt is not None and len(rt):
            row = rt[rt["period"] == "0m"]
            if len(row):
                row = row.iloc[0]
                out["analyst_breakdown"] = {k: int(row.get(k) or 0) for k in
                                            ("strongBuy", "buy", "hold", "sell", "strongSell")}
    except Exception:
        pass
    try:
        cal = t.calendar
        if isinstance(cal, dict):
            ed = cal.get("Earnings Date") or []
            if ed:
                out["earnings_date"] = parse_date(ed[0]).isoformat() if parse_date(ed[0]) else None
    except Exception:
        pass
    news = []
    try:
        for n in (t.news or [])[: n_news + 2]:
            c = n.get("content") if isinstance(n.get("content"), dict) else n
            title = c.get("title")
            link = ((c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url")
                    or c.get("link"))
            if title and link:
                news.append({"title": title[:150], "link": link,
                             "publisher": (c.get("provider") or {}).get("displayName") or c.get("publisher") or "",
                             "date": str(c.get("pubDate") or n.get("providerPublishTime") or "")[:10]})
    except Exception:
        pass
    out["news"] = news[:n_news]
    out["insiders"] = _insiders(t)
    return out


def enrich(tickers, cache, max_age_h=20, n_news=5, pause=0.6, status=None):
    """Actualiza cache para los tickers dados (solo los caducados). Devuelve cache."""
    done = errors = 0
    now = now_utc()
    for tk in tickers:
        c = cache.get(tk)
        if c and c.get("fetched_at"):
            try:
                age = (now - dt.datetime.fromisoformat(c["fetched_at"])).total_seconds() / 3600
                if age < max_age_h:
                    continue
            except Exception:
                pass
        try:
            cache[tk] = fetch_one(tk, n_news)
            done += 1
        except Exception as e:
            errors += 1
            log(f"enrich {tk}: {e}")
            if "Too Many Requests" in str(e) or "Rate" in str(e):
                log("Yahoo limita peticiones: paro el enriquecimiento en esta ejecucion")
                break
        time.sleep(pause)
    if status:
        if errors and not done:
            status.fail("Yahoo (fundamentales/noticias)", f"{errors} errores", done)
        else:
            status.ok("Yahoo (fundamentales/noticias)", done, f"{errors} errores" if errors else "")
    return cache


def upside(price, target):
    if price and target:
        return rnd((target / price - 1) * 100, 1)
    return None


def enrich_earnings(tickers, cache, max_age_days=20, pause=0.6, status=None, limit=60):
    """Historico de reacciones a resultados (cambia poco: cache ~20 dias)."""
    done = errors = 0
    now = now_utc()
    for tk in tickers[:limit]:
        c = cache.setdefault(tk, {})
        eh = c.get("earn_hist")
        if eh and eh.get("fetched_at"):
            try:
                if (now - dt.datetime.fromisoformat(eh["fetched_at"])).days < max_age_days:
                    continue
            except Exception:
                pass
        try:
            c["earn_hist"] = fetch_earnings_history(tk)
            done += 1
        except Exception as e:
            errors += 1
            log(f"earnings {tk}: {e}")
            if "Too Many Requests" in str(e) or "Rate" in str(e):
                break
        time.sleep(pause)
    if status:
        status.ok("Yahoo (historico de resultados)", done, f"{errors} errores" if errors else "")
    return cache
