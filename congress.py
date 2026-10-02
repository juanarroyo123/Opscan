"""Operaciones de congresistas (Camara + Senado) y altos cargos del ejecutivo.

Fuente de trades: kadoa-org/congress-trading-monitor (MIT) que agrega House Clerk,
Senate eFD y OGE.  Comites: unitedstates/congress-legislators (dominio publico).
Recordatorio: la STOCK Act permite declarar hasta 45 dias despues -> la senal llega tarde,
usala como sesgo de sector, no como momento de entrada.
"""
import datetime as dt
import re
import unicodedata

from .util import get, get_json, iso_now, log, num, parse_date

TRADES_URL = "https://raw.githubusercontent.com/kadoa-org/congress-trading-monitor/main/public/data/trades.json"
LEG_BASE = "https://raw.githubusercontent.com/unitedstates/congress-legislators/main/"

# comite -> sectores GICS en los que tiene informacion / poder regulatorio
COMMITTEE_SECTORS = [
    (r"armed services|defense|homeland|intelligence", ["Industrials", "Information Technology"]),
    (r"energy and commerce", ["Health Care", "Energy", "Communication Services", "Information Technology", "Utilities"]),
    (r"energy|natural resources|environment", ["Energy", "Utilities", "Materials"]),
    (r"financial services|banking|finance|ways and means|budget", ["Financials", "Real Estate"]),
    (r"health|aging|veterans", ["Health Care"]),
    (r"agriculture", ["Consumer Staples", "Materials"]),
    (r"commerce|science|technology|judiciary|strategic competition", ["Information Technology", "Communication Services"]),
    (r"transportation|infrastructure|public works", ["Industrials"]),
    (r"small business", ["Consumer Discretionary"]),
]


def _norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z ]", " ", s.lower()).split()


SUFFIX = {"jr", "sr", "ii", "iii", "iv", "md", "hon", "dr", "mr", "mrs", "ms"}


def last_name(full):
    toks = [t for t in _norm(full) if t not in SUFFIX and len(t) > 1]
    return toks[-1] if toks else ""


def committee_sectors(name):
    out = set()
    low = (name or "").lower()
    for pat, secs in COMMITTEE_SECTORS:
        if re.search(pat, low):
            out.update(secs)
    return sorted(out)


def build_member_index(legislators, membership, committees):
    """-> {(last, state, chamber): {"name", "committees":[...], "sectors":[...]}}"""
    cname = {}
    for c in committees or []:
        cname[c["thomas_id"]] = c["name"]
        for sc in c.get("subcommittees") or []:
            cname[c["thomas_id"] + sc["thomas_id"]] = c["name"] + " / " + sc["name"]
    by_bio = {}
    for cid, members in (membership or {}).items():
        parent = cid[:4]
        for mbr in members or []:
            b = mbr.get("bioguide")
            if not b:
                continue
            nm = cname.get(parent) or cname.get(cid) or cid
            role = mbr.get("title")
            lst = by_bio.setdefault(b, {})
            if nm not in lst or role:
                lst[nm] = role
    idx = {}
    for l in legislators or []:
        term = (l.get("terms") or [{}])[-1]
        chamber = "senate" if term.get("type") == "sen" else "house"
        nm = l.get("name", {})
        b = l.get("id", {}).get("bioguide")
        comms = by_bio.get(b, {})
        secs = set()
        for c in comms:
            secs.update(committee_sectors(c))
        rec = {"name": nm.get("official_full") or f"{nm.get('first','')} {nm.get('last','')}",
               "committees": [c + (f" ({r})" if r else "") for c, r in comms.items()],
               "sectors": sorted(secs), "party": (term.get("party") or "")[:1]}
        idx[(last_name(nm.get("last", "")), term.get("state"), chamber)] = rec
    return idx


def direction(type_raw):
    t = (type_raw or "").lower()
    if "purchase" in t or t == "buy":
        return "Compra"
    if "sale" in t or "sell" in t:
        return "Venta"
    if "exchange" in t:
        return "Canje"
    return "Otro"


def amount_mid(r):
    lo, hi = num(r.get("amount_range_low")), num(r.get("amount_range_high"))
    if lo and hi:
        return (lo + hi) / 2
    return lo or hi or 0


def normalize_trades(raw, member_idx, include_executive=True, horizon_days=365, today=None):
    today = today or dt.date.today()
    out, seen = [], set()
    for r in raw or []:
        branch = r.get("branch")
        if branch == "executive" and not include_executive:
            continue
        tk = (r.get("ticker") or "").strip().upper()
        if not tk or tk in ("--", "N/A") or not re.match(r"^[A-Z][A-Z0-9.\-]{0,6}$", tk):
            continue
        td = parse_date(r.get("transaction_date"))
        if not td:
            continue
        age = (today - td).days
        if age < 0 or age > horizon_days:
            continue
        chamber = r.get("chamber") or ("executive" if branch == "executive" else "")
        key = (r.get("id") or "", tk, td, r.get("filer_name"), r.get("transaction_type"), r.get("amount_range_label"))
        if key in seen:
            continue
        seen.add(key)
        info = member_idx.get((last_name(r.get("filer_name")), r.get("state"), chamber)) if chamber in ("house", "senate") else None
        asset_type = (r.get("asset_type") or "")
        out.append({
            "ticker": tk.replace("-", "."), "company": re.sub(r"\s+", " ", r.get("asset_name") or tk)[:80],
            "member": r.get("filer_name") or "-", "chamber": {"house": "Camara", "senate": "Senado"}.get(chamber, "Ejecutivo"),
            "party": r.get("party") or (info or {}).get("party") or "", "state": r.get("state") or "",
            "office": r.get("office") or r.get("agency") or "", "owner": r.get("owner") or "",
            "direction": direction(r.get("transaction_type")), "type_raw": r.get("transaction_type") or "",
            "is_option": "option" in asset_type.lower() or asset_type.upper() == "OP",
            "amount": r.get("amount_range_label") or "-", "amount_mid": amount_mid(r),
            "transaction_date": td.isoformat(), "filing_date": (r.get("filing_date") or "")[:10] or None,
            "days_to_file": r.get("days_to_file"), "late": bool(r.get("is_late")),
            "days": age, "link": r.get("doc_url"),
            "committees": (info or {}).get("committees", [])[:8],
            "committee_sectors": (info or {}).get("sectors", []),
            "ret_since": r.get("ret_since"), "excess_since": r.get("excess_since"),
        })
    out.sort(key=lambda x: x["transaction_date"], reverse=True)
    return out


def summarize_by_ticker(trades, sectors, window_days=90, cluster_days=30, max_filer_trades=60):
    """Resumen por valor: compras/ventas, miembros distintos, relevancia de comite, cluster.
    Quien declara mas de `max_filer_trades` operaciones en la ventana suele tener una cartera
    gestionada por terceros (cientos de compras automaticas): no cuenta como senal."""
    counts = {}
    for t in trades:
        if t["days"] <= window_days:
            counts[t["member"]] = counts.get(t["member"], 0) + 1
    noisy = {m for m, n in counts.items() if n > max_filer_trades}
    out = {}
    for t in trades:
        if t["days"] > window_days or t["member"] in noisy:
            continue
        s = out.setdefault(t["ticker"], {"buys": 0, "sells": 0, "buy_usd": 0.0, "sell_usd": 0.0,
                                         "buyers": set(), "sellers": set(), "recent_buyers": set(),
                                         "committee_buyers": set(), "option_buys": 0, "last": None})
        sector = sectors.get(t["ticker"], "")
        if t["direction"] == "Compra":
            s["buys"] += 1; s["buy_usd"] += t["amount_mid"]; s["buyers"].add(t["member"])
            if t["days"] <= cluster_days:
                s["recent_buyers"].add(t["member"])
            if sector and sector in t["committee_sectors"]:
                s["committee_buyers"].add(t["member"])
            if t["is_option"]:
                s["option_buys"] += 1
        elif t["direction"] == "Venta":
            s["sells"] += 1; s["sell_usd"] += t["amount_mid"]; s["sellers"].add(t["member"])
        if not s["last"] or t["transaction_date"] > s["last"]:
            s["last"] = t["transaction_date"]
    for tk, s in out.items():
        nb, ns = len(s["buyers"]), len(s["sellers"])
        s["n_buyers"], s["n_sellers"] = nb, ns
        s["cluster"] = len(s["recent_buyers"]) >= 2
        s["committee_relevant"] = len(s["committee_buyers"]) > 0
        tot = s["buy_usd"] + s["sell_usd"]
        s["bias"] = round((s["buy_usd"] - s["sell_usd"]) / tot, 2) if tot else 0.0
        for k in ("buyers", "sellers", "recent_buyers", "committee_buyers"):
            s[k] = sorted(s[k])[:10]
        s["buy_usd"], s["sell_usd"] = round(s["buy_usd"]), round(s["sell_usd"])
    return out


def build(cfg, status, sectors, today=None):
    cc, sc = cfg["congress"], cfg["scoring"]
    try:
        raw = get_json(TRADES_URL, timeout=120)
        status.ok("Congreso (kadoa: Camara+Senado+OGE)", len(raw))
    except Exception as e:
        status.fail("Congreso (kadoa: Camara+Senado+OGE)", e)
        return None
    idx = {}
    try:
        import yaml
        leg = yaml.safe_load(get(LEG_BASE + "legislators-current.yaml", timeout=60).text)
        mem = yaml.safe_load(get(LEG_BASE + "committee-membership-current.yaml", timeout=60).text)
        com = yaml.safe_load(get(LEG_BASE + "committees-current.yaml", timeout=60).text)
        idx = build_member_index(leg, mem, com)
        status.ok("Comites del Congreso", len(idx))
    except Exception as e:
        status.fail("Comites del Congreso", e)
    trades = normalize_trades(raw, idx, cc["include_executive"], cc["horizon_days"], today)
    matched = sum(1 for t in trades if t["committees"])
    log(f"Congreso: {len(trades)} trades con ticker, {matched} con comites asociados")
    summary = summarize_by_ticker(trades, sectors, sc["congress_window_days"], sc["congress_cluster_days"],
                                  int(cc.get("max_filer_trades", 60)))
    counts = {}
    for t in trades:
        if t["days"] <= sc["congress_window_days"]:
            counts[t["member"]] = counts.get(t["member"], 0) + 1
    noisy = sorted([m for m, n in counts.items() if n > int(cc.get("max_filer_trades", 60))])
    for t in trades:
        t["managed"] = t["member"] in noisy
    clusters = sorted(
        [{"ticker": k, **{x: v[x] for x in ("buys", "sells", "n_buyers", "buy_usd", "recent_buyers",
                                              "committee_buyers", "cluster", "bias", "last")}}
         for k, v in summary.items() if v["n_buyers"] >= 2 or v["committee_relevant"]],
        key=lambda x: (x["cluster"], x["n_buyers"], x["buy_usd"]), reverse=True)[:60]
    latest_filing = max((t["filing_date"] or "" for t in trades), default="")
    return {"generated_at": iso_now(), "count": len(trades), "latest_filing": latest_filing,
            "window_days": sc["congress_window_days"], "trades": trades[: cc["max_rows"]],
            "by_ticker": summary, "clusters": clusters, "managed_filers": noisy,
            "note": "Fuente: House Clerk + Senate eFD + OGE via kadoa-org/congress-trading-monitor. "
                    "Importes en rangos declarados. Retraso legal de hasta 45 dias. "
                    "Quien declara mas de 60 operaciones en 90 dias (cartera gestionada) no cuenta como senal."}
