"""Calendario de catalizadores: FDA (PDUFA, AdCom), lecturas de ensayos, resultados,
IPOs, M&A (8-K), FOMC/macro, vencimientos (OPEX) y entradas manuales.

Registro normalizado: {date, type, ticker, company, title, source, url, days, extra}
"""
import csv
import datetime as dt
import io
import os
import re

from .config import CONFIG_DIR
from .util import get, get_json, iso_now, log, parse_date, sec_user_agent, third_friday

PDUFA_API = "https://www.pdufa.bio/api/v1"
FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"

# Fechas de decision FOMC publicadas por la Fed (segundo dia de la reunion).
FOMC_STATIC = ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29",
               "2026-09-16", "2026-10-28", "2026-12-09"]

MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july",
                                       "august", "september", "october", "november", "december"], 1)}


def rec(date, type_, ticker="", company="", title="", source="", url="", **extra):
    return {"date": date, "type": type_, "ticker": (ticker or "").upper(), "company": company or "",
            "title": (title or "")[:160], "source": source, "url": url or "", "extra": extra or None}


# ------------------------------------------------------------------ manual
def load_manual(path=None):
    path = path or os.path.join(CONFIG_DIR, "catalysts_manual.csv")
    out = []
    if not os.path.exists(path):
        return out
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            tk = (row.get("ticker") or "").strip().upper()
            d = parse_date(row.get("date"))
            if tk and d:
                out.append(rec(d.isoformat(), (row.get("type") or "Manual").strip(), tk, tk,
                               (row.get("note") or "").strip(), "Manual"))
    return out


# ------------------------------------------------------------------ pdufa.bio
def parse_pdufa_bio(payload, kind):
    out = []
    for r in (payload or {}).get("data") or []:
        d = parse_date(r.get("date") or r.get("decision_date") or r.get("meeting_date"))
        if not d:
            continue
        if (r.get("status") or "").lower() in ("decided", "completed", "cancelled", "canceled"):
            continue
        typ = r.get("type") or kind
        if kind == "pdufa":
            typ = "PDUFA" if not r.get("type") else r["type"]
        elif kind == "readouts":
            typ = "Lectura ensayo"
        elif kind == "adcomm":
            typ = "AdCom (FDA)"
        title = r.get("name") or r.get("title") or r.get("drug") or ""
        if r.get("indication"):
            title += " - " + str(r["indication"])
        extra = {k: r.get(k) for k in ("date_precision", "market_cap_tier", "cash_runway_months",
                                        "cohort_move_median_pct", "cohort_move_p25_pct",
                                        "cohort_move_p75_pct", "therapeutic_area") if r.get(k) is not None}
        out.append(rec(d.isoformat(), typ, r.get("ticker") or "", r.get("company") or "",
                       title, "pdufa.bio", r.get("url") or "https://www.pdufa.bio", **extra))
    return out


def fetch_pdufa_bio():
    out = []
    for kind in ("pdufa", "readouts", "adcomm"):
        offset = 0
        for _ in range(6):
            p = get_json(f"{PDUFA_API}/{kind}", params={"limit": 500, "offset": offset}, timeout=40, retries=2)
            out += parse_pdufa_bio(p, kind)
            meta = p.get("meta") or {}
            ret = meta.get("returned") or len(p.get("data") or [])
            offset += ret
            if not ret or offset >= (meta.get("total") or 0):
                break
    return out


# ------------------------------------------------------------------ Finnhub
def parse_finnhub_earnings(payload):
    out = []
    hours = {"bmo": "antes de apertura", "amc": "tras el cierre", "dmh": "en sesion"}
    for e in (payload or {}).get("earningsCalendar", []) or []:
        d = parse_date(e.get("date"))
        sym = (e.get("symbol") or "").upper()
        if not d or not sym:
            continue
        h = hours.get(e.get("hour") or "", "")
        out.append(rec(d.isoformat(), "Resultados", sym, sym, "Resultados" + (f" ({h})" if h else ""),
                       "Finnhub", "", hour=e.get("hour"), eps_estimate=e.get("epsEstimate"),
                       revenue_estimate=e.get("revenueEstimate"), quarter=e.get("quarter")))
    return out


def parse_finnhub_ipo(payload):
    out = []
    for e in (payload or {}).get("ipoCalendar", []) or []:
        d = parse_date(e.get("date"))
        if not d:
            continue
        out.append(rec(d.isoformat(), "IPO", e.get("symbol") or "", e.get("name") or "",
                       f"IPO {e.get('exchange') or ''} {e.get('price') or ''}".strip(), "Finnhub"))
    return out


def fetch_finnhub(horizon):
    token = os.environ.get("FINNHUB_TOKEN")
    if not token:
        raise RuntimeError("sin FINNHUB_TOKEN (opcional)")
    start = dt.date.today()
    out = []
    # en tramos de 30 dias para no truncar
    for i in range(0, horizon, 30):
        a = (start + dt.timedelta(days=i)).isoformat()
        b = (start + dt.timedelta(days=min(i + 29, horizon))).isoformat()
        out += parse_finnhub_earnings(get_json("https://finnhub.io/api/v1/calendar/earnings",
                                               params={"from": a, "to": b, "token": token}))
    out += parse_finnhub_ipo(get_json("https://finnhub.io/api/v1/calendar/ipo",
                                      params={"from": start.isoformat(),
                                              "to": (start + dt.timedelta(days=horizon)).isoformat(),
                                              "token": token}))
    return out


# ------------------------------------------------------------------ ClinicalTrials.gov
_SUFFIX = r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|llc|lp|sa|ag|nv|se|holdings?|group|the)\b"


def norm_company(name):
    s = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    s = re.sub(_SUFFIX, " ", s)
    return re.sub(r"\s+", " ", s).strip()


def load_sec_name_map():
    data = get_json(SEC_TICKERS, headers={"User-Agent": sec_user_agent()}, timeout=40)
    out = {}
    for v in data.values():
        n = norm_company(v.get("title"))
        if n and n not in out:
            out[n] = v.get("ticker", "").upper()
    return out


def parse_clinicaltrials(payload, allowed_phases=None, name_map=None):
    out = []
    for st in (payload or {}).get("studies", []):
        ps = st.get("protocolSection", {})
        idm, stm = ps.get("identificationModule", {}), ps.get("statusModule", {})
        dm, spm = ps.get("designModule", {}), ps.get("sponsorCollaboratorsModule", {})
        d = parse_date((stm.get("primaryCompletionDateStruct") or {}).get("date"))
        if not d:
            continue
        phases = dm.get("phases", []) or []
        if allowed_phases and not any(p in allowed_phases for p in phases):
            continue
        sponsor = (spm.get("leadSponsor") or {}).get("name", "")
        tk = (name_map or {}).get(norm_company(sponsor), "")
        nct = idm.get("nctId", "")
        phase = ", ".join(p.replace("PHASE", "Fase ") for p in phases) or "N/A"
        out.append(rec(d.isoformat(), "Ensayo " + phase, tk, sponsor, idm.get("briefTitle", ""),
                       "ClinicalTrials.gov", ("https://clinicaltrials.gov/study/" + nct) if nct else ""))
    return out


def fetch_clinicaltrials(ccfg, name_map):
    start = dt.date.today().isoformat()
    end = (dt.date.today() + dt.timedelta(days=ccfg["horizon_days"])).isoformat()
    params = {
        "filter.advanced": f"AREA[PrimaryCompletionDate]RANGE[{start},{end}] AND AREA[LeadSponsorClass]INDUSTRY",
        "pageSize": 1000, "countTotal": "false",
        "fields": "NCTId|BriefTitle|Phase|PrimaryCompletionDate|LeadSponsorName",
    }
    out, token = [], None
    for _ in range(10):
        if token:
            params["pageToken"] = token
        data = get_json("https://clinicaltrials.gov/api/v2/studies", params=params, timeout=60)
        out += [r for r in parse_clinicaltrials(data, set(ccfg["trial_phases"]), name_map) if r["ticker"]]
        token = data.get("nextPageToken")
        if not token:
            break
    return out


# ------------------------------------------------------------------ SEC EDGAR (M&A)
def parse_sec_edgar(payload, term=""):
    out = []
    for h in ((payload or {}).get("hits") or {}).get("hits", []):
        src = h.get("_source", {})
        d = parse_date(src.get("file_date"))
        names = src.get("display_names", []) or []
        company = names[0] if names else ""
        m = re.search(r"\(([A-Z]{1,6}(?:[.-][A-Z])?)\)", company)
        ticker = m.group(1) if m else ""
        cik = re.search(r"CIK (\d+)", company)
        url = (f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik.group(1)}&type=8-K"
               if cik else "")
        clean = re.sub(r"\s*\(CIK.*$", "", company)
        clean = re.sub(r"\s*\([A-Z.\-, ]{1,20}\)\s*$", "", clean).strip()
        if d:
            out.append(rec(d.isoformat(), "M&A / 8-K", ticker, clean,
                           f"8-K: {term}" if term else "8-K", "SEC EDGAR", url))
    return out


def fetch_sec(ccfg):
    out = []
    start = (dt.date.today() - dt.timedelta(days=7)).isoformat()
    for term in ccfg["sec_terms"]:
        data = get_json("https://efts.sec.gov/LATEST/search-index",
                        params={"q": f'"{term}"', "forms": "8-K", "dateRange": "custom", "startdt": start,
                                "enddt": dt.date.today().isoformat()},
                        headers={"User-Agent": sec_user_agent()}, timeout=40)
        out += parse_sec_edgar(data, term)
    return out


# ------------------------------------------------------------------ Macro / FOMC / OPEX
def parse_fomc_html(html):
    """Extrae fechas de decision (ultimo dia de cada reunion) de la pagina de la Fed."""
    out = []
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    for ym in re.finditer(r"(20\d\d) FOMC Meetings(.*?)(?=20\d\d FOMC Meetings|$)", text):
        year = int(ym.group(1))
        for m in re.finditer(r"(January|February|March|April|May|June|July|August|September|October|"
                             r"November|December)(?:/(\w+))?\s+(\d{1,2})(?:-(\d{1,2}))?\*?", ym.group(2)):
            mon = MONTHS[m.group(1).lower()]
            if m.group(2) and m.group(2).lower() in MONTHS:
                mon = MONTHS[m.group(2).lower()]
            day = int(m.group(4) or m.group(3))
            try:
                out.append(dt.date(year, mon, day).isoformat())
            except ValueError:
                pass
    return sorted(set(out))


def fomc_events():
    dates = set(FOMC_STATIC)
    try:
        dates.update(parse_fomc_html(get(FOMC_URL, timeout=30).text))
    except Exception as e:
        log(f"FOMC web: {e} (uso fechas fijas)")
    return [rec(d, "Macro", "", "Reserva Federal", "Decision de tipos FOMC + rueda de prensa", "Fed",
                FOMC_URL) for d in sorted(dates)]


def opex_events(horizon, today=None):
    today = today or dt.date.today()
    out = []
    y, m = today.year, today.month
    for _ in range(int(horizon / 28) + 2):
        tf = third_friday(y, m)
        quarterly = m in (3, 6, 9, 12)
        out.append(rec(tf.isoformat(), "OPEX", "", "Mercado",
                       "Vencimiento trimestral (cuadruple hechizo)" if quarterly else "Vencimiento mensual de opciones",
                       "Calculado"))
        # VIX: miercoles 30 dias antes del 3er viernes del mes siguiente
        ny, nm = (y + (m // 12), m % 12 + 1)
        vix = third_friday(ny, nm) - dt.timedelta(days=30)
        out.append(rec(vix.isoformat(), "OPEX", "VIX", "Cboe", "Vencimiento de opciones/futuros VIX", "Calculado"))
        y, m = ny, nm
    return out


def fetch_fmp_macro(horizon):
    token = os.environ.get("FMP_TOKEN")
    if not token:
        raise RuntimeError("sin FMP_TOKEN (opcional)")
    a = dt.date.today().isoformat()
    b = (dt.date.today() + dt.timedelta(days=min(horizon, 90))).isoformat()
    data = get_json("https://financialmodelingprep.com/stable/economic-calendar",
                    params={"from": a, "to": b, "apikey": token}, timeout=40)
    out = []
    for e in data or []:
        if (e.get("country") or "").upper() not in ("US", "USA"):
            continue
        if (e.get("impact") or "").lower() not in ("high", "medium"):
            continue
        d = parse_date(e.get("date"))
        if d:
            out.append(rec(d.isoformat(), "Macro", "", "EEUU",
                           f"{e.get('event')} (impacto {e.get('impact')})", "FMP"))
    return out


# ------------------------------------------------------------------ orquestacion
def finalize(records, horizon, today=None):
    today = today or dt.date.today()
    clean, seen = [], set()
    for r in records:
        d = parse_date(r.get("date"))
        if not d:
            continue
        days = (d - today).days
        if days < -2 or days > horizon:
            continue
        key = (d, r["type"], r["ticker"], (r["company"] or "")[:25].lower(), (r["title"] or "")[:30].lower())
        if key in seen:
            continue
        seen.add(key)
        r = dict(r)
        r["date"], r["days"] = d.isoformat(), days
        clean.append(r)
    clean.sort(key=lambda x: (x["days"], x["type"], x["ticker"]))
    return clean


def by_ticker(events):
    out = {}
    for e in events:
        if e["ticker"] and e["days"] >= 0:
            out.setdefault(e["ticker"], []).append(e)
    return out


def build(cfg, status):
    ccfg = cfg["calendar"]
    horizon = ccfg["horizon_days"]
    records = load_manual()
    status.ok("Catalizadores manuales", len(records))

    def run(name, fn):
        try:
            r = fn() or []
            status.ok(name, len(r))
            return r
        except Exception as e:
            status.fail(name, e)
            return []

    records += run("pdufa.bio (PDUFA/lecturas/AdCom)", fetch_pdufa_bio)
    records += run("Finnhub (resultados/IPO)", lambda: fetch_finnhub(horizon))
    if ccfg.get("include_clinicaltrials"):
        name_map = {}
        try:
            name_map = load_sec_name_map()
        except Exception as e:
            log(f"SEC company_tickers: {e}")
        records += run("ClinicalTrials.gov", lambda: fetch_clinicaltrials(ccfg, name_map))
    if ccfg.get("include_sec"):
        records += run("SEC EDGAR (M&A)", lambda: fetch_sec(ccfg))
    records += run("FOMC", fomc_events)
    records += run("FMP macro", lambda: fetch_fmp_macro(horizon))
    if ccfg.get("include_opex"):
        records += opex_events(horizon)
    final = finalize(records, horizon)
    counts = {}
    for r in final:
        counts[r["type"]] = counts.get(r["type"], 0) + 1
    return {"generated_at": iso_now(), "horizon_days": horizon, "count": len(final),
            "by_type": counts, "events": final}
