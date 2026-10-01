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
