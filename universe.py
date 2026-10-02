"""Universo de valores: S&P 500 + Nasdaq 100 + ETFs + indices + watchlist + biotech con catalizador."""
import csv
import io
import os
import re

from .config import CONFIG_DIR
from .util import get, log

SP500_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
NDX_WIKI = "https://en.wikipedia.org/wiki/Nasdaq-100"
IWB_URL = ("https://www.ishares.com/us/products/239707/ishares-russell-1000-etf/1467271812596.ajax"
           "?fileType=csv&fileName=IWB_holdings&dataType=fund")

INDEX_SECTOR = "Indice"
ETF_SECTOR = "ETF"


def _read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def parse_sp500_csv(text):
    out = {}
    for r in csv.DictReader(io.StringIO(text)):
        t = (r.get("Symbol") or r.get("ticker") or "").strip().upper()
        if t:
            out[t] = {"name": r.get("Security") or r.get("name") or t,
                      "sector": r.get("GICS Sector") or r.get("sector") or "",
                      "industry": r.get("GICS Sub-Industry") or r.get("industry") or ""}
    return out


def load_sp500(status=None):
    try:
        data = parse_sp500_csv(get(SP500_URL, timeout=30).text)
        if len(data) > 400:
            if status:
                status.ok("Universo S&P 500", len(data))
            return data
        raise ValueError(f"solo {len(data)} filas")
    except Exception as e:
        rows = _read_csv(os.path.join(CONFIG_DIR, "sp500.csv"))
        data = {r["ticker"].upper(): {"name": r.get("name", ""), "sector": r.get("sector", ""),
                                      "industry": r.get("industry", "")} for r in rows}
        if status:
            status.ok("Universo S&P 500", len(data), "lista local (fuente online no disponible, no afecta)")
        return data


def load_nasdaq100(status=None):
    try:
        import pandas as pd
        html = get(NDX_WIKI, timeout=30).text
        tables = pd.read_html(io.StringIO(html))
        for t in tables:
            cols = [str(c).lower() for c in t.columns]
            if any("ticker" in c or "symbol" in c for c in cols) and len(t) >= 90:
                tcol = t.columns[[i for i, c in enumerate(cols) if "ticker" in c or "symbol" in c][0]]
                scol = next((t.columns[i] for i, c in enumerate(cols) if "sector" in c), None)
                data = {}
                for _, r in t.iterrows():
                    tk = str(r[tcol]).strip().upper()
                    if tk and tk != "NAN":
                        data[tk] = {"sector": str(r[scol]) if scol is not None else ""}
                if status:
                    status.ok("Universo Nasdaq 100", len(data))
                return data
        raise ValueError("tabla no encontrada")
    except Exception as e:
        rows = _read_csv(os.path.join(CONFIG_DIR, "nasdaq100.csv"))
        data = {r["ticker"].upper(): {"sector": r.get("sector", "")} for r in rows}
        if status:
            status.ok("Universo Nasdaq 100", len(data), "lista local (Wikipedia no disponible, no afecta)")
        return data


def parse_ishares_csv(text, known=None):
    """CSV de posiciones de iShares (con lineas de cabecera antes de 'Ticker,...')."""
    known = known or set()
    lines = (text or "").splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith("Ticker,") or l.startswith('"Ticker"')), None)
    if start is None:
        return {}
    out = {}
    for r in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
        tk = (r.get("Ticker") or "").strip().upper()
        if not tk or tk == "-" or (r.get("Asset Class") or "Equity").strip() != "Equity":
            continue
        if not re.match(r"^[A-Z][A-Z0-9.]{0,6}$", tk):
            continue
        # iShares quita el punto de las clases (BRKB -> BRK.B)
        if tk not in known and len(tk) >= 3 and (tk[:-1] + "." + tk[-1]) in known:
            tk = tk[:-1] + "." + tk[-1]
        out[tk] = {"name": (r.get("Name") or "").title(), "sector": (r.get("Sector") or "").strip()}
    return out


SSGA_URL = "https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/holdings-daily-us-en-{fund}.xlsx"


def parse_ssga_holdings(df_raw):
    """Excel de posiciones de SPDR (cabecera en una fila con 'Ticker')."""
    hdr = None
    for i in range(min(len(df_raw), 30)):
        row = [str(x).strip() for x in df_raw.iloc[i].tolist()]
        if "Ticker" in row:
            hdr = i
            break
    if hdr is None:
        return {}
    cols = [str(x).strip() for x in df_raw.iloc[hdr].tolist()]
    out = {}
    for _, r in df_raw.iloc[hdr + 1:].iterrows():
        rec = dict(zip(cols, r.tolist()))
        tk = str(rec.get("Ticker") or "").strip().upper()
        if not tk or tk in ("NAN", "-", "CASH_USD") or not re.match(r"^[A-Z][A-Z0-9.]{0,6}$", tk):
            continue
        out[tk] = {"name": str(rec.get("Name") or "").title(), "sector": str(rec.get("Sector") or "").strip()}
    return out


def load_ssga(fund, label, status=None, min_rows=300):
    try:
        import pandas as pd
        r = get(SSGA_URL.format(fund=fund.lower()), timeout=60)
        df = pd.read_excel(io.BytesIO(r.content), header=None)
        data = parse_ssga_holdings(df)
        if len(data) < min_rows:
            raise ValueError(f"solo {len(data)} valores (respuesta: {r.headers.get('content-type')})")
        if status:
            status.ok(label, len(data))
        return data
    except Exception as e:
        if status:
            status.fail(label, e)
        return {}


def load_russell1000(status=None, known=None):
    try:
        data = parse_ishares_csv(get(IWB_URL, timeout=60).text, known)
        if len(data) < 800:
            raise ValueError(f"solo {len(data)} valores")
        if status:
            status.ok("Universo Russell 1000 (iShares IWB)", len(data))
        return data
    except Exception as e:
        if status:
            status.fail("Universo Russell 1000 (iShares IWB)", e)
        return {}


def build_universe(cfg, status=None, catalyst_tickers=None, offline=False, low_activity=None):
    """Devuelve dict ticker -> {name, sector, industry, groups:[...]} ordenado por prioridad."""
    u = cfg["universe"]
    meta = {}

    def add(tk, group, **kw):
        tk = tk.strip().upper()
        if not tk or tk in u["exclude"]:
            return
        m = meta.setdefault(tk, {"name": "", "sector": "", "industry": "", "groups": []})
        if group not in m["groups"]:
            m["groups"].append(group)
        for k, v in kw.items():
            if v and not m.get(k):
                m[k] = v

    # prioridad: watchlist > indices/ETF > catalizadores > S&P > NDX
    for t in u["watchlist"]:
        add(t, "watchlist")
    for t in u["indices"]:
        add(t, "indice", sector=INDEX_SECTOR, name=t)
    for t in u["etfs"]:
        add(t, "etf", sector=ETF_SECTOR, name=t)
    if u.get("add_catalyst_tickers") and catalyst_tickers:
        for t in sorted(catalyst_tickers):
            add(t, "catalizador")
    if offline:
        sp = {r["ticker"].upper(): r for r in _read_csv(os.path.join(CONFIG_DIR, "sp500.csv"))}
        ndx = {r["ticker"].upper(): r for r in _read_csv(os.path.join(CONFIG_DIR, "nasdaq100.csv"))}
    else:
        sp = load_sp500(status) if u.get("sp500") else {}
        ndx = load_nasdaq100(status) if u.get("nasdaq100") else {}
    for t, r in sp.items():
        add(t, "sp500", name=r.get("name"), sector=r.get("sector"), industry=r.get("industry"))
    for t, r in ndx.items():
        add(t, "ndx", sector=r.get("sector"))
    skipped = 0
    extra = {}
    if u.get("midcap400") and not offline:
        extra.update(load_ssga("MDY", "Universo S&P 400 MidCap (SPDR MDY)", status))
    if u.get("russell1000") and not offline:
        extra.update(load_russell1000(status, set(sp) | set(ndx)))
    if extra:
        low = low_activity or set()
        for t, r in extra.items():
            if t in meta:
                continue
            if t in low:
                skipped += 1   # casi sin volumen de opciones: no merece la pena escanearlo
                continue
            add(t, "ampliado", name=r.get("name"), sector=r.get("sector"))
        if skipped:
            log(f"Universo: {skipped} valores del Russell 1000 omitidos por poca actividad en opciones")
    # sector de valores del watchlist/catalizador si estan en S&P
    for t, m in meta.items():
        if not m["sector"] and t in sp:
            m["sector"] = sp[t].get("sector", "")
    keys = list(meta.keys())[: int(u.get("max_tickers") or 900)]
    log(f"Universo: {len(keys)} valores")
    return {k: meta[k] for k in keys}
