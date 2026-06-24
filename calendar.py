#!/usr/bin/env python3
"""
calendar.py - calendario masivo de catalizadores desde fuentes gratuitas.

Agrega, normaliza y unifica en docs/data/catalysts.json los catalizadores de:
  - ClinicalTrials.gov v2  (lecturas de ensayos)        API oficial, sin clave
  - SEC EDGAR full-text    (M&A, 8-K, eventos)          API oficial, sin clave
  - Finnhub                (earnings, IPO)               free tier, necesita FINNHUB_TOKEN
  - FDA.gov                (comites AdCom)               scrape best-effort
  - FMP                    (calendario macro)            opcional, necesita FMP_TOKEN
  - config/catalysts.csv   (manual: PDUFA, etc.)         lo que no tiene API

Modos (config/watchlist.json -> "calendar_mode"):
  "market"    -> trae todo el mercado (luego filtras en la web)
  "watchlist" -> solo catalizadores de tus tickers

Diseno: las funciones parse_*() son PURAS (reciben el payload crudo y devuelven
registros normalizados) para poder testearlas sin red. Las fetch_*() hacen la
llamada HTTP. Cada fuente va envuelta en try/except: si una falla, las demas siguen.

Registro normalizado:
  {date, type, ticker, company, title, source, url}
"""
import os
import re
import csv
import json
import datetime as dt
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")
DATA_DIR = os.path.join(ROOT, "docs", "data")
OUT_JSON = os.path.join(DATA_DIR, "catalysts.json")

UA = {"User-Agent": "OpScan/1.0 (calendario educativo; contacto: opscan@example.com)"}

DEFAULTS = {
    "calendar_mode": "market",       # "market" o "watchlist"
    "calendar_horizon_days": 90,     # ventana hacia adelante
    "trial_phases": ["PHASE2", "PHASE3"],
    "sec_terms": ["merger agreement", "tender offer", "acquisition agreement"],
    "tickers": [],
}


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def load_config():
    cfg = dict(DEFAULTS)
    path = os.path.join(CONFIG_DIR, "watchlist.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        for k in DEFAULTS:
            if user.get(k) is not None:
                cfg[k] = user[k]
        cfg["tickers"] = [t.strip().upper() for t in user.get("tickers", [])]
    return cfg


def load_manual():
    path = os.path.join(CONFIG_DIR, "catalysts.csv")
    out = []
    if not os.path.exists(path):
        return out
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("ticker") and row.get("date"):
                out.append({
                    "date": row["date"].strip(),
                    "type": (row.get("type") or "Manual").strip(),
                    "ticker": row["ticker"].strip().upper(),
                    "company": row["ticker"].strip().upper(),
                    "title": (row.get("note") or "").strip(),
                    "source": "Manual",
                    "url": "",
                })
    return out


def _iso(s):
    """Normaliza varias formas de fecha a YYYY-MM-DD, o None."""
    if not s:
        return None
    s = str(s)[:10]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y", "%Y-%m"):
        try:
            d = dt.datetime.strptime(s if fmt != "%Y-%m" else s[:7], fmt).date()
            return d.isoformat()
        except ValueError:
            continue
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    return m.group(0) if m else None


def days_until(iso):
    try:
        return (dt.date.fromisoformat(iso) - dt.date.today()).days
    except Exception:
        return None


# ==========================================================================
# PARSERS PUROS (testeables sin red)
# ==========================================================================
def parse_clinicaltrials(payload):
    out = []
    for st in (payload or {}).get("studies", []):
        ps = st.get("protocolSection", {})
        idm = ps.get("identificationModule", {})
        stm = ps.get("statusModule", {})
        dm = ps.get("designModule", {})
        spm = ps.get("sponsorCollaboratorsModule", {})
        nct = idm.get("nctId", "")
        d = _iso((stm.get("primaryCompletionDateStruct") or {}).get("date"))
        if not d:
            continue
        phases = dm.get("phases", []) or []
        phase = ", ".join(p.replace("PHASE", "Fase ") for p in phases) or "N/A"
        sponsor = (spm.get("leadSponsor") or {}).get("name", "")
        out.append({
            "date": d,
            "type": f"Ensayo {phase}",
            "ticker": "",
            "company": sponsor,
            "title": idm.get("briefTitle", "")[:140],
            "source": "ClinicalTrials.gov",
            "url": f"https://clinicaltrials.gov/study/{nct}" if nct else "",
        })
    return out


def parse_sec_edgar(payload, term=""):
    out = []
    hits = ((payload or {}).get("hits") or {}).get("hits", [])
    for h in hits:
        src = h.get("_source", {})
        d = _iso(src.get("file_date"))
        names = src.get("display_names", []) or []
        company = names[0] if names else ""
        ticker = ""
        m = re.search(r"\(([A-Z]{1,6})\)", company)
        if m:
            ticker = m.group(1)
        adsh = (h.get("_id", "").split(":")[0]).replace("-", "")
        cik = re.search(r"CIK (\d+)", company)
        url = ""
        if cik and adsh:
            url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik.group(1)}&type=8-K"
        out.append({
            "date": d,
            "type": "M&A / 8-K",
            "ticker": ticker,
            "company": re.sub(r"\s*\(CIK.*$", "", company).strip(),
            "title": (src.get("forms", ["8-K"])[0] if src.get("forms") else "8-K") + (f" · {term}" if term else ""),
            "source": "SEC EDGAR",
            "url": url,
        })
    return [r for r in out if r["date"]]


def parse_finnhub_earnings(payload):
    out = []
    for e in (payload or {}).get("earningsCalendar", []):
        d = _iso(e.get("date"))
        if not d:
            continue
        out.append({
            "date": d, "type": "Earnings", "ticker": (e.get("symbol") or "").upper(),
            "company": (e.get("symbol") or "").upper(),
            "title": "Resultados" + (f" ({e.get('hour')})" if e.get("hour") else ""),
            "source": "Finnhub", "url": "",
        })
    return out


def parse_finnhub_ipo(payload):
    out = []
    for e in (payload or {}).get("ipoCalendar", []):
        d = _iso(e.get("date"))
        if not d:
            continue
        out.append({
            "date": d, "type": "IPO", "ticker": (e.get("symbol") or "").upper(),
            "company": e.get("name", ""),
            "title": f"IPO {e.get('exchange','')} {e.get('price','')}".strip(),
            "source": "Finnhub", "url": "",
        })
    return out


def parse_fmp_econ(payload):
    out = []
    for e in (payload or []):
        d = _iso(e.get("date"))
        if not d:
            continue
        if (e.get("country") or "").upper() not in ("US", "USD", "ESTADOS UNIDOS", ""):
            continue
        out.append({
            "date": d, "type": "Macro", "ticker": "",
            "company": e.get("country", ""),
            "title": f"{e.get('event','')} (imp: {e.get('impact','')})".strip(),
            "source": "FMP", "url": "",
        })
    return out


def parse_fda_adcom_tables(dfs):
    """Recibe lista de DataFrames (pandas.read_html) del calendario FDA. Best-effort."""
    out = []
    for df in dfs or []:
        cols = [str(c).lower() for c in df.columns]
        date_col = next((df.columns[i] for i, c in enumerate(cols) if "date" in c), None)
        title_col = next((df.columns[i] for i, c in enumerate(cols) if "committee" in c or "meeting" in c or "title" in c), None)
        if date_col is None:
            continue
        for _, r in df.iterrows():
            d = _iso(r.get(date_col))
            if not d:
                continue
            out.append({
                "date": d, "type": "AdCom (FDA)", "ticker": "",
                "company": "",
                "title": str(r.get(title_col, ""))[:140] if title_col is not None else "Reunion comite FDA",
                "source": "FDA.gov", "url": "https://www.fda.gov/advisory-committees/advisory-committee-calendar",
            })
    return out


# ==========================================================================
# FETCHERS (red) -- envueltos en try/except por el caller
# ==========================================================================
def _get(url, params=None, headers=None, timeout=30):
    import requests
    r = requests.get(url, params=params, headers=headers or UA, timeout=timeout)
    r.raise_for_status()
    return r


def fetch_clinicaltrials(cfg):
    import requests  # noqa
    start = date.today().isoformat()
    end = (date.today() + timedelta(days=cfg["calendar_horizon_days"])).isoformat()
    phases = "|".join(cfg["trial_phases"])
    params = {
        "filter.advanced": f"AREA[PrimaryCompletionDate]RANGE[{start},{end}] AND AREA[Phase]({phases})",
        "filter.overallStatus": "RECRUITING|ACTIVE_NOT_RECRUITING|ENROLLING_BY_INVITATION",
        "pageSize": 200,
        "fields": "NCTId|BriefTitle|Phase|PrimaryCompletionDate|OverallStatus|LeadSponsorName",
    }
    out, token, pages = [], None, 0
    while pages < 5:
        if token:
            params["pageToken"] = token
        r = _get("https://clinicaltrials.gov/api/v2/studies", params=params)
        data = r.json()
        out += parse_clinicaltrials(data)
        token = data.get("nextPageToken")
        pages += 1
        if not token:
            break
    return out


def fetch_sec_edgar(cfg):
    out = []
    start = (date.today() - timedelta(days=7)).isoformat()
    end = date.today().isoformat()
    for term in cfg["sec_terms"]:
        try:
            r = _get("https://efts.sec.gov/LATEST/search-index",
                     params={"q": f'"{term}"', "forms": "8-K", "startdt": start, "enddt": end})
            out += parse_sec_edgar(r.json(), term=term)
        except Exception as e:
            print(f"    SEC '{term}': {e}")
    return out


def fetch_finnhub(cfg):
    token = os.environ.get("FINNHUB_TOKEN")
    if not token:
        print("    Finnhub: sin FINNHUB_TOKEN, se omite")
        return []
    start = date.today().isoformat()
    end = (date.today() + timedelta(days=cfg["calendar_horizon_days"])).isoformat()
    out = []
    try:
        r = _get("https://finnhub.io/api/v1/calendar/earnings",
                 params={"from": start, "to": end, "token": token})
        out += parse_finnhub_earnings(r.json())
    except Exception as e:
        print(f"    Finnhub earnings: {e}")
    try:
        r = _get("https://finnhub.io/api/v1/calendar/ipo",
                 params={"from": start, "to": end, "token": token})
        out += parse_finnhub_ipo(r.json())
    except Exception as e:
        print(f"    Finnhub IPO: {e}")
    return out


def fetch_fda_adcom():
    import pandas as pd
    dfs = pd.read_html("https://www.fda.gov/advisory-committees/advisory-committee-calendar")
    return parse_fda_adcom_tables(dfs)


def fetch_fmp_econ(cfg):
    token = os.environ.get("FMP_TOKEN")
    if not token:
        print("    FMP macro: sin FMP_TOKEN, se omite")
        return []
    start = date.today().isoformat()
    end = (date.today() + timedelta(days=cfg["calendar_horizon_days"])).isoformat()
    r = _get("https://financialmodelingprep.com/api/v3/economic_calendar",
             params={"from": start, "to": end, "apikey": token})
    return parse_fmp_econ(r.json())


# ==========================================================================
# Orquestacion
# ==========================================================================
def collect(cfg):
    sources = [
        ("ClinicalTrials.gov", lambda: fetch_clinicaltrials(cfg)),
        ("SEC EDGAR", lambda: fetch_sec_edgar(cfg)),
        ("Finnhub", lambda: fetch_finnhub(cfg)),
        ("FDA AdCom", fetch_fda_adcom),
        ("FMP macro", lambda: fetch_fmp_econ(cfg)),
    ]
    records = load_manual()
    for name, fn in sources:
        print(f"  · {name} ...")
        try:
            recs = fn() or []
            print(f"    {len(recs)} registros")
            records += recs
        except Exception as e:
            print(f"    ERROR en {name}: {e}")
    return records


def finalize(records, cfg):
    today = date.today()
    horizon = cfg["calendar_horizon_days"]
    wl = set(cfg["tickers"])
    wl_names = {t for t in wl}

    clean, seen = [], set()
    for r in records:
        d = _iso(r.get("date"))
        if not d:
            continue
        du = days_until(d)
        if du is None or du < -2 or du > horizon:
            continue
        if cfg["calendar_mode"] == "watchlist":
            tk = (r.get("ticker") or "").upper()
            comp = (r.get("company") or "").upper()
            if tk not in wl and not any(n in comp for n in wl_names if n):
                continue
        key = (d, r.get("type"), r.get("ticker"), (r.get("company") or "")[:30], (r.get("title") or "")[:40])
        if key in seen:
            continue
        seen.add(key)
        r["date"] = d
        r["days"] = du
        clean.append(r)

    clean.sort(key=lambda x: x["days"])
    return clean


def main():
    cfg = load_config()
    print(f"Calendario · modo={cfg['calendar_mode']} · horizonte={cfg['calendar_horizon_days']}d")
    records = collect(cfg)
    final = finalize(records, cfg)

    by_type = {}
    for r in final:
        by_type[r["type"]] = by_type.get(r["type"], 0) + 1

    out = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "mode": cfg["calendar_mode"],
        "horizon_days": cfg["calendar_horizon_days"],
        "count": len(final),
        "by_type": by_type,
        "events": final,
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"Escrito {OUT_JSON}: {len(final)} eventos · {by_type}")


if __name__ == "__main__":
    main()
