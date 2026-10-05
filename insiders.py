"""Compras de directivos (SEC Formulario 4) para todo el universo.

Cuando varios directivos compran acciones de su propia empresa con su dinero (codigo "P",
compra en mercado abierto) en pocos dias, es una de las senales con mejor historial.
Las ventas son casi siempre rutinarias (impuestos, planes 10b5-1) y solo se muestran.

Fuente (gratis): buscador de EDGAR (efts.sec.gov) para listar los Form 4 de cada dia y
el XML de cada uno en www.sec.gov/Archives. Cache incremental en state/insiders.json.
"""
import datetime as dt
import time
import xml.etree.ElementTree as ET

from .util import get, get_json, log, sec_user_agent

SEARCH = "https://efts.sec.gov/LATEST/search-index"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{fname}"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
KEEP_DAYS = 120


def _h():
    return {"User-Agent": sec_user_agent(), "Accept-Encoding": "gzip, deflate"}


def _txt(node, path):
    if node is None:
        return None
    el = node.find(path)
    if el is None:
        return None
    v = el.find("value")
    v = v if v is not None else el
    return (v.text or "").strip() or None


def _num(x):
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_form4(xml_text):
    """XML del Form 4 -> {ticker, issuer_cik, owner, title, roles, tx:[...]}"""
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    iss = root.find("issuer")
    own = root.find("reportingOwner")
    rel = own.find("reportingOwnerRelationship") if own is not None else None
    flag = lambda k: (_txt(rel, k) or "0").lower() in ("1", "true")
    title = _txt(rel, "officerTitle") or ""
    roles = []
    if flag("isDirector"):
        roles.append("Consejero")
    if flag("isOfficer"):
        roles.append(title or "Directivo")
    if flag("isTenPercentOwner"):
        roles.append("Accionista >10%")
    out = {"ticker": (_txt(iss, "issuerTradingSymbol") or "").upper().strip(),
           "issuer_cik": (_txt(iss, "issuerCik") or "").lstrip("0"),
           "owner": _txt(own, "reportingOwnerId/rptOwnerName") if own is not None else None,
           "roles": roles, "is_officer": flag("isOfficer"), "is_director": flag("isDirector"),
           "plan_10b5": (_txt(root, "aff10b5One") or "0") in ("1", "true"), "tx": []}
    for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _txt(t, "transactionCoding/transactionCode")
        sh = _num(_txt(t, "transactionAmounts/transactionShares"))
        px = _num(_txt(t, "transactionAmounts/transactionPricePerShare"))
        out["tx"].append({"date": _txt(t, "transactionDate"), "code": code, "shares": sh, "price": px,
                          "ad": _txt(t, "transactionAmounts/transactionAcquiredDisposedCode"),
                          "value": round(sh * px) if sh and px else None})
    return out


def load_cik_map():
    """ticker -> cik (sin ceros a la izquierda)"""
    data = get_json(SEC_TICKERS, headers=_h(), timeout=40)
    return {str(v.get("ticker", "")).upper(): str(v.get("cik_str")) for v in data.values()}


def search_day(day, pause=0.12):
    """Todos los Form 4 presentados ese dia: [(ciks, adsh, fname)]"""
    out, start, total = [], 0, None
    while total is None or start < min(total, 9900):
        j = get_json(SEARCH, params={"forms": "4", "dateRange": "custom", "startdt": day, "enddt": day,
                                     "from": start}, headers=_h(), timeout=40)
        hits = (j.get("hits") or {})
        total = int(((hits.get("total") or {}).get("value")) or 0)
        rows = hits.get("hits") or []
        for h in rows:
            src = h.get("_source") or {}
            _id = h.get("_id") or ""
            adsh, _, fname = _id.partition(":")
            if adsh and fname and str(src.get("form", "4")) == "4":
                out.append(([str(c).lstrip("0") for c in src.get("ciks") or []], adsh, fname))
        if len(rows) < 100:
            break
        start += 100
        time.sleep(pause)
    return out


def _bdays_back(today, n):
    d, out = today, []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= dt.timedelta(days=1)
    return out


def build(tickers, cache, status=None, days=25, max_fetch=1200, pause=0.12, today=None):
    """Actualiza la cache con los Form 4 nuevos de los valores del universo."""
    today = today or dt.date.today()
    cache = dict(cache or {})
    seen = dict(cache.get("seen") or {})
    done = set(cache.get("days_done") or [])
    txs = list(cache.get("tx") or [])
    try:
        cik_map = load_cik_map()
    except Exception as e:
        if status:
            status.fail("SEC Form 4 (directivos)", e)
        return cache
    want = {cik_map[t]: t for t in tickers if t in cik_map}
    fetched = errors = 0
    recent = set(_bdays_back(today, 2))          # hoy y ayer: se vuelven a mirar (llegan durante el dia)
    try:
        for day in _bdays_back(today, days):
            if day in done and day not in recent:
                continue
            if fetched >= max_fetch:
                break
            queue = []
            for ciks, adsh, fname in search_day(day, pause):
                if adsh in seen:
                    continue
                cik = next((c for c in ciks if c in want), None)
                if cik:
                    queue.append((cik, adsh, fname))
            complete = True
            for cik, adsh, fname in queue:
                if fetched >= max_fetch:
                    complete = False
                    break
                url = ARCHIVE.format(cik=cik, acc=adsh.replace("-", ""), fname=fname)
                try:
                    f = parse_form4(get(url, headers=_h(), timeout=30).text)
                    tk = f["ticker"] if f["ticker"] in tickers else want.get(cik)
                    for t in f["tx"]:
                        if t["code"] in ("P", "S") and t.get("date"):
                            txs.append({"ticker": tk, "date": t["date"], "filed": day, "code": t["code"],
                                        "owner": f["owner"], "roles": f["roles"], "officer": f["is_officer"],
                                        "director": f["is_director"], "plan": f["plan_10b5"],
                                        "shares": t["shares"], "price": t["price"], "value": t["value"],
                                        "adsh": adsh})
                    seen[adsh] = day
                except Exception as e:
                    errors += 1
                    if errors <= 3:
                        log(f"Form 4 {adsh}: {e}")
                fetched += 1
                time.sleep(pause)
            if complete:
                done.add(day)
    except Exception as e:
        log(f"Form 4: {e}")
        if status:
            status.fail("SEC Form 4 (directivos)", e)
    cutoff = (today - dt.timedelta(days=KEEP_DAYS)).isoformat()
    txs = [t for t in txs if (t.get("date") or "") >= cutoff]
    # sin duplicados (mismo formulario y misma linea)
    uniq = {}
    for t in txs:
        uniq[(t["adsh"], t["date"], t["code"], t["shares"], t["price"])] = t
    cache = {"updated": dt.datetime.utcnow().isoformat(timespec="seconds"),
             "seen": {k: v for k, v in seen.items() if v >= cutoff},
             "days_done": sorted(d for d in done if d >= cutoff), "tx": list(uniq.values())}
    if status and fetched >= 0:
        n_buy = sum(1 for t in cache["tx"] if t["code"] == "P")
        status.ok("SEC Form 4 (directivos)", fetched, f"{n_buy} compras en cache" + (f", {errors} errores" if errors else ""))
    log(f"Form 4: {fetched} formularios leidos, {len(cache['tx'])} operaciones en cache")
    return cache


def summarize(cache, ticker, today=None, window=30, min_value=10000):
    """Resumen por valor: compras (lo que cuenta) y ventas (contexto)."""
    today = today or dt.date.today()
    since = (today - dt.timedelta(days=window)).isoformat()
    tx = [t for t in (cache or {}).get("tx", []) if t.get("ticker") == ticker and (t.get("date") or "") >= since]
    if not tx:
        return None
    buys = [t for t in tx if t["code"] == "P" and (t.get("value") or 0) >= min_value]
    sells = [t for t in tx if t["code"] == "S"]
    buyers = {}
    for t in buys:
        b = buyers.setdefault(t.get("owner") or "?", {"owner": t.get("owner"), "roles": t.get("roles") or [],
                                                     "value": 0, "last": t["date"]})
        b["value"] += t.get("value") or 0
        b["last"] = max(b["last"], t["date"])
    return {"window_days": window, "buys": len(buys), "buyers": len(buyers),
            "buy_usd": round(sum(t.get("value") or 0 for t in buys)),
            "sells": len(sells), "sellers": len({t.get("owner") for t in sells}),
            "sell_usd": round(sum(t.get("value") or 0 for t in sells)),
            "officer_buy": any(t.get("officer") for t in buys),
            "top_buyers": sorted(buyers.values(), key=lambda x: -x["value"])[:5],
            "last_buy": max((t["date"] for t in buys), default=None)}


def score(summary):
    """Puntos (0-10) por compras de directivos en 30 dias."""
    if not summary or not summary.get("buys"):
        return 0, None
    n, usd = summary["buyers"], summary["buy_usd"]
    if n >= 3 or usd >= 1_000_000:
        p = 10
    elif n >= 2 or usd >= 250_000:
        p = 7
    elif usd >= 100_000 and summary.get("officer_buy"):
        p = 4
    else:
        p = 2
    who = ", ".join(f"{(b['owner'] or '?').title()} ({(b['roles'] or ['?'])[0]})" for b in summary["top_buyers"][:2])
    why = f"{n} directivo(s) compraron ${usd/1e3:,.0f}k en {summary['window_days']}d: {who}"
    return p, why
