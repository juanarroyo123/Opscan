"""Contexto tecnico de la ACCION (para confirmar el flujo de opciones):
medias de 20/50 dias, volumen relativo de la accion, maximos de 52 semanas.

Fuente: Yahoo (descarga diaria en bloques) con cache en state/tech.json;
si Yahoo falla, se calcula con el historico propio (state/ticker_daily.csv).
"""
import datetime as dt

from .util import log, now_utc, rnd


def features_from_series(close, volume=None, today_volume=None):
    """close/volume: listas (antiguo -> reciente) de cierres diarios ANTERIORES a hoy."""
    c = [float(x) for x in close if x == x and x]
    if len(c) < 20:
        return None
    last = c[-1]
    sma20 = sum(c[-20:]) / 20
    sma50 = sum(c[-50:]) / 50 if len(c) >= 50 else None
    out = {"last_close": rnd(last, 2), "sma20": rnd(sma20, 2), "sma50": rnd(sma50, 2),
           "above20": last > sma20, "above50": (last > sma50) if sma50 else None,
           "chg_5d": rnd((last / c[-6] - 1) * 100, 2) if len(c) > 6 else None,
           "chg_20d": rnd((last / c[-21] - 1) * 100, 2) if len(c) > 21 else None}
    hi = max(c[-252:])
    out["dist_52w_high"] = rnd((last / hi - 1) * 100, 1)
    if volume:
        v = [float(x) for x in volume if x == x and x]
        if len(v) >= 20:
            avg = sum(v[-20:]) / 20
            out["avg_vol20"] = round(avg)
            ref = today_volume if today_volume else v[-1]
            out["rel_stock_vol"] = rnd(ref / avg, 2) if avg else None
    return out


def fetch_yahoo(tickers, chunk=150):
    """Devuelve {ticker: {"close": [...], "volume": [...]}} con ~1 ano de historia."""
    import yfinance as yf
    from .options import yf_symbol
    out = {}
    tickers = list(tickers)
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        sym = {yf_symbol(t): t for t in part}
        try:
            df = yf.download(list(sym.keys()), period="1y", interval="1d", group_by="column",
                             auto_adjust=False, progress=False, threads=True)
        except Exception as e:
            log(f"tecnico: bloque {i} fallo: {e}")
            continue
        if df is None or not len(df):
            continue
        for s, tk in sym.items():
            try:
                close = df["Close"][s].dropna() if len(sym) > 1 else df["Close"].dropna()
                vol = df["Volume"][s].dropna() if len(sym) > 1 else df["Volume"].dropna()
                today = dt.date.today()
                close = close[[d.date() < today for d in close.index]]
                vol = vol[[d.date() < today for d in vol.index]]
                if len(close) >= 20:
                    out[tk] = {"close": [round(float(x), 4) for x in close.tolist()][-260:],
                               "volume": [float(x) for x in vol.tolist()][-260:]}
            except Exception:
                continue
    return out


def build(tickers, cache, status=None, max_age_h=18):
    """Actualiza la cache de series si es vieja. cache = {"fetched_at":..., "series": {...}}"""
    fresh = False
    if cache.get("fetched_at"):
        try:
            age = (now_utc() - dt.datetime.fromisoformat(cache["fetched_at"])).total_seconds() / 3600
            fresh = age < max_age_h
        except Exception:
            pass
    missing = [t for t in tickers if t not in (cache.get("series") or {})]
    if fresh and len(missing) < 50:
        return cache
    try:
        series = fetch_yahoo(tickers)
        if len(series) < len(tickers) * 0.3:
            raise RuntimeError(f"solo {len(series)} de {len(tickers)} valores")
        cache = {"fetched_at": now_utc().isoformat(timespec="seconds"), "series": series}
        if status:
            status.ok("Yahoo (tecnico de acciones)", len(series))
    except Exception as e:
        if status:
            status.fail("Yahoo (tecnico de acciones)", e)
    return cache


def features(ticker, cache, daily=None, today_volume=None):
    s = (cache.get("series") or {}).get(ticker)
    if s:
        return features_from_series(s["close"], s.get("volume"), today_volume)
    if daily is not None and len(daily):
        h = daily[daily["ticker"] == ticker].sort_values("date")
        if len(h) >= 20:
            return features_from_series(h["price"].tolist()[:-1], h["stock_volume"].tolist()[:-1], today_volume)
    return None
