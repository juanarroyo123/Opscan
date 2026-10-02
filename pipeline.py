"""Orquestador de OpScan.

Modos:
  full       cierre (todo: calendario, congreso, futuros, opciones, Yahoo, alertas)
  premarket  antes de apertura (todo; confirma por OI el flujo de ayer con el OI nuevo)
  intraday   solo opciones (reutiliza calendario/congreso/futuros ya generados)
  smoke      prueba rapida con pocos valores (para comprobar que todas las fuentes responden)
"""
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import alerts, catalysts, congress, enrich, futures, options, scoring, state, universe
from .config import DOCS_DIR, data_dir, load_config
from .util import Status, iso_now, log, parse_date, read_json, rnd, today_et, write_json

YAHOO_TO_GICS = {
    "Technology": "Information Technology", "Healthcare": "Health Care",
    "Financial Services": "Financials", "Consumer Cyclical": "Consumer Discretionary",
    "Consumer Defensive": "Consumer Staples", "Basic Materials": "Materials",
}
FULL_MODES = ("full", "premarket", "smoke")


def _out(name):
    return os.path.join(data_dir(), name)


def scan_one(tk, ocfg, pending_syms):
    """Descarga y analiza un valor. Devuelve dict (sin la cadena completa para ahorrar memoria)."""
    t0 = time.time()
    und, contracts = options.parse_cboe(options.fetch_cboe(tk), tk)
    if not contracts:
        raise RuntimeError("Cboe sin contratos")
    m, unusual = options.analyze_chain(und, contracts, ocfg)
    oi_map = {c["symbol"]: c["oi"] for c in contracts if c["symbol"] in pending_syms}
    m["stock_volume"] = und.get("stock_volume")
    m["iv30_change"] = und.get("iv30_change")
    return {"ticker": tk, "metrics": m, "unusual": unusual, "oi_map": oi_map, "secs": round(time.time() - t0, 2)}


def scan_one_yf(tk, ocfg, pending_syms):
    und, contracts = options.fetch_yfinance(tk, ocfg["max_dte"])
    if not contracts:
        raise RuntimeError("Yahoo sin contratos")
    m, unusual = options.analyze_chain(und, contracts, ocfg)
    oi_map = {c["symbol"]: c["oi"] for c in contracts if c["symbol"] in pending_syms}
    return {"ticker": tk, "metrics": m, "unusual": unusual, "oi_map": oi_map, "secs": 0}


def scan_options(tickers, cfg, flags, status, allow_fallback=True):
    ocfg = cfg["options"]
    pending = {}
    if len(flags):
        p = flags[flags["status"] == "PENDIENTE"]
        for tk, sym in zip(p["ticker"], p["symbol"]):
            pending.setdefault(tk, set()).add(sym)
    results, failed = {}, {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=int(ocfg["workers"])) as ex:
        futs = {ex.submit(scan_one, tk, ocfg, pending.get(tk, set())): tk for tk in tickers}
        for i, f in enumerate(as_completed(futs), 1):
            tk = futs[f]
            try:
                results[tk] = f.result()
            except Exception as e:
                failed[tk] = str(e)[:200]
            if i % 100 == 0:
                log(f"  opciones: {i}/{len(tickers)} ({time.time()-t0:.0f}s)")
    status.ok("Cboe opciones", len(results), f"{len(failed)} fallos" if failed else "")
    # respaldo Yahoo
    fb = list(failed.keys())[: int(ocfg["yfinance_fallback_max"])] if allow_fallback else []
    ok_fb = 0
    for tk in fb:
        try:
            results[tk] = scan_one_yf(tk, ocfg, pending.get(tk, set()))
            failed.pop(tk, None)
            ok_fb += 1
        except Exception as e:
            failed[tk] = f"Cboe y Yahoo fallaron: {str(e)[:120]}"
        time.sleep(0.5)
    if fb:
        status.ok("Yahoo opciones (respaldo)", ok_fb, f"{len(fb) - ok_fb} sin datos")
    log(f"Opciones: {len(results)} ok, {len(failed)} sin datos, {time.time()-t0:.0f}s")
    if not results:
        status.fail("Cboe opciones", "ningun valor descargado")
    return results, failed


def _sector(meta, enr):
    s = (meta or {}).get("sector") or ""
    if not s and enr and enr.get("sector"):
        s = YAHOO_TO_GICS.get(enr["sector"], enr["sector"])
    return s


def build_record(tk, res, meta, base, fs, cats, cong, fctx, enr, cfg):
    m = dict(res["metrics"])
    m["_unusual"] = res["unusual"]
    sector = _sector(meta, enr)
    fut_bias, fut_why = fctx.get(sector) or fctx.get("_default") or (None, [])
    s = scoring.score_ticker(m, base, fs, cats, cong, fut_bias, fut_why, enr, cfg)
    e = enr or {}
    rec = {
        "ticker": tk, "name": e.get("name") or (meta or {}).get("name") or tk, "sector": sector,
        "groups": (meta or {}).get("groups", []),
        **{k: m.get(k) for k in ("price", "change_pct", "source", "session_date", "n_expirations",
                                 "n_contracts", "opt_vol", "call_vol", "put_vol", "call_oi", "put_oi",
                                 "cp_vol_ratio", "cp_oi_ratio", "pc_vol_ratio", "vol_oi_total",
                                 "call_premium", "put_premium", "bull_premium", "bear_premium",
                                 "flow_bias", "unusual_count", "unusual_premium", "effective_premium", "whales", "iv30",
                                 "iv30_change", "iv_front", "front_premium", "iv_term", "max_pain",
                                 "max_pain_exp", "call_wall", "put_wall", "gex_usd_1pct")},
        "expected_moves": (m.get("expected_moves") or [])[:6],
        "rel_vol": base.get("rel_vol"), "avg_opt_vol": base.get("avg_opt_vol"),
        "iv_rank": base.get("iv_rank"), "iv_pct": base.get("iv_pct"), "history_days": base.get("history_days"),
        "unusual": res["unusual"][:10],
        "oi_flags": {k: fs[k] for k in ("confirmed", "not_confirmed", "pending")},
        "confirmed": fs["confirmed_list"],
        "catalysts": [{k: c.get(k) for k in ("date", "days", "type", "title", "source", "url")} for c in (cats or [])[:6]],
        "congress": ({k: cong.get(k) for k in ("buys", "sells", "n_buyers", "n_sellers", "buy_usd", "sell_usd",
                                                "buyers", "sellers", "committee_buyers", "cluster",
                                                "committee_relevant", "bias", "last")} if cong else None),
        "futures_ctx": {"bias": fut_bias, "why": fut_why},
        "fund": {k: e.get(k) for k in ("market_cap", "short_pct_float", "inst_pct", "target_mean",
                                       "target_low", "target_high", "recommendation", "analysts",
                                       "analyst_breakdown", "beta")} if e else None,
        "upside_pct": enrich.upside(m.get("price"), e.get("target_mean")) if e else None,
        "insiders": e.get("insiders") if e else None,
        "news": e.get("news") if e else None,
        **s,
    }
    return rec


def run(mode="full", tickers_override=None, offline_universe=False):
    t_start = time.time()
    cfg = load_config()
    status = Status()
    os.makedirs(data_dir(), exist_ok=True)
    log(f"OpScan modo={mode} datos={data_dir()}")
    full = mode in FULL_MODES

    # 1) calendario
    if full:
        cal = catalysts.build(cfg, status)
        write_json(_out("catalysts.json"), cal)
    else:
        cal = read_json(_out("catalysts.json"), {"events": []})
    cal_by_tk = catalysts.by_ticker(cal.get("events", []))
    cat_tickers = {e["ticker"] for e in cal.get("events", [])
                   if e["ticker"] and any(k in e["type"] for k in ("PDUFA", "AdCom", "Lectura", "Manual"))}

    # 2) universo
    if tickers_override:
        uni = {t.upper(): {"name": t.upper(), "sector": "", "groups": ["manual"]} for t in tickers_override}
    elif mode == "smoke":
        base_u = universe.build_universe(cfg, None, None, offline=True)
        pick = (cfg["universe"]["watchlist"][:4] + ["SPY", "SPX", "AAPL", "NVDA"])
        uni = {t: base_u.get(t, {"name": t, "sector": "", "groups": ["smoke"]}) for t in pick}
    else:
        uni = universe.build_universe(cfg, status, cat_tickers, offline=offline_universe)
    sectors = {t: m.get("sector", "") for t, m in uni.items()}

    # 3) futuros
    if full:
        fut = futures.build(cfg, status)
        fut["generated_at"] = iso_now()
        write_json(_out("futures.json"), fut)
    else:
        fut = read_json(_out("futures.json"), None)
    links = cfg["futures"].get("sector_links") or {}
    fctx = {"_default": futures.sector_context(fut, "", {})}
    for sec in set(sectors.values()) | set(links.keys()):
        fctx[sec] = futures.sector_context(fut, sec, links)

    # 4) congreso
    if full:
        pol = congress.build(cfg, status, sectors)
        if pol:
            write_json(_out("political.json"), pol)
        else:
            pol = read_json(_out("political.json"), None)
    else:
        pol = read_json(_out("political.json"), None)
    cong_by = (pol or {}).get("by_ticker", {})

    # 5) opciones
    flags = state.load_flags()
    daily = state.load_daily()
    results, failed = scan_options(list(uni.keys()), cfg, flags, status, allow_fallback=full or bool(tickers_override))
    sessions = [r["metrics"]["session_date"] for r in results.values() if r["metrics"].get("session_date")]
    session = max(set(sessions), key=sessions.count) if sessions else today_et().isoformat()
    for tk, res in results.items():
        sd = res["metrics"].get("session_date") or session
        flags = state.confirm_flags(flags, tk, res["oi_map"], sd, cfg["options"]["oi_confirm_ratio"])
        flags = state.add_flags(flags, res["unusual"], sd)
    flags = state.prune_flags(flags)
    daily = state.upsert_daily(daily, [r["metrics"] for r in results.values()])

    # 6) puntuacion preliminar -> enriquecer top N -> puntuacion final
    ecache = state.load_cache("enrich.json", {})
    bases, fsums = {}, {}
    for tk, res in results.items():
        m = res["metrics"]
        bases[tk] = state.baselines(daily, tk, m.get("session_date") or session, m.get("opt_vol"), m.get("iv30"))
        fsums[tk] = state.flag_summary(flags, tk, m.get("session_date") or session,
                                       cfg["options"]["oi_confirm_window"])
    prelim = []
    for tk, res in results.items():
        r = build_record(tk, res, uni.get(tk), bases[tk], fsums[tk], cal_by_tk.get(tk),
                         cong_by.get(tk), fctx, ecache.get(tk), cfg)
        prelim.append((r["score"], tk))
    if full:
        prelim.sort(reverse=True)
        want = [tk for _, tk in prelim[: int(cfg["enrich"]["top_n"])]]
        want += [t for t in cfg["universe"]["watchlist"] if t in results and t not in want]
        want = [t for t in want if t not in options.INDEXES
                and not ({"etf", "indice"} & set((uni.get(t) or {}).get("groups", [])))]
        ecache = enrich.enrich(want, ecache, n_news=int(cfg["enrich"]["news"]), status=status)
        state.save_cache("enrich.json", ecache)
        # resultados desde Yahoo para valores sin fecha en el calendario
        for tk in want:
            ed = (ecache.get(tk) or {}).get("earnings_date")
            if ed and not any(c["type"] == "Resultados" for c in cal_by_tk.get(tk, [])):
                d = parse_date(ed)
                if d:
                    days = (d - today_et()).days
                    if 0 <= days <= cfg["calendar"]["horizon_days"]:
                        cal_by_tk.setdefault(tk, []).append(
                            {"date": d.isoformat(), "days": days, "type": "Resultados", "ticker": tk,
                             "title": "Resultados (Yahoo)", "source": "Yahoo", "url": ""})
                        cal_by_tk[tk].sort(key=lambda c: c["days"])

    records = []
    for tk, res in results.items():
        records.append(build_record(tk, res, uni.get(tk), bases[tk], fsums[tk], cal_by_tk.get(tk),
                                    cong_by.get(tk), fctx, ecache.get(tk), cfg))
    for tk, err in failed.items():
        records.append({"ticker": tk, "name": (uni.get(tk) or {}).get("name") or tk,
                        "sector": sectors.get(tk, ""), "score": 0, "signal": "ERR", "direction": "-",
                        "error": err, "reasons": [], "checklist": {}, "unusual": []})
    records.sort(key=lambda r: r.get("score") or 0, reverse=True)

    # 7) flujo global
    flow = []
    dir_of = {r["ticker"]: r.get("direction") for r in records}
    for tk, res in results.items():
        for u in res["unusual"]:
            flow.append({**u, "sector": sectors.get(tk, ""), "ticker_direction": dir_of.get(tk)})
    flow.sort(key=lambda u: (u["score"], u["premium"]), reverse=True)
    confirmed_recent = []
    if len(flags):
        cf = flags[flags["status"] == "CONFIRMADA"].sort_values("checked_date", ascending=False).head(300)
        for _, r in cf.iterrows():
            confirmed_recent.append({k: (None if str(r[k]) == "nan" else r[k]) for k in
                                     ("date", "checked_date", "ticker", "symbol", "kind", "strike", "expiration",
                                      "volume", "oi_before", "oi_after", "premium", "direction", "side")})

    # 8) alertas
    sent = state.load_cache("alerts.json", {})
    sent = alerts.process([r for r in records if r.get("signal") != "ERR"], cfg, sent, session, status)
    state.save_cache("alerts.json", sent)

    # 9) escribir
    ok = [r for r in records if r.get("signal") != "ERR"]
    summary = {
        "valores": len(records), "con_datos": len(ok), "alta": sum(1 for r in ok if r["signal"] == "ALTA"),
        "media": sum(1 for r in ok if r["signal"] == "MEDIA"),
        "entradas": sum(1 for r in ok if r.get("checklist", {}).get("entrada")),
        "pre_alertas": sum(1 for r in ok if r.get("checklist", {}).get("pre_alerta")),
        "inusuales": sum(r.get("unusual_count") or 0 for r in ok),
        "alcistas": sum(1 for r in ok if r["direction"] == "ALCISTA" and r["signal"] != "BAJA"),
        "bajistas": sum(1 for r in ok if r["direction"] == "BAJISTA" and r["signal"] != "BAJA"),
    }
    gen = iso_now()
    write_json(_out("latest.json"), {"generated_at": gen, "mode": mode, "session_date": session,
                                     "summary": summary, "regime": (fut or {}).get("regime"),
                                     "config": {"scoring": cfg["scoring"], "options": cfg["options"]},
                                     "records": records})
    write_json(_out("flow.json"), {"generated_at": gen, "session_date": session,
                                   "count": len(flow), "rows": flow[: int(cfg["options"]["top_flow_rows"])],
                                   "confirmed": confirmed_recent})
    state.save_flags(flags)
    state.save_daily(daily)
    elapsed = round(time.time() - t_start, 1)
    prev = read_json(_out("status.json"), {}) or {}
    runs = (prev.get("runs") or [])[-30:]
    runs.append({"at": gen, "mode": mode, "secs": elapsed, "ok": len(results), "failed": len(failed)})
    write_json(_out("status.json"), {"generated_at": gen, "mode": mode, "session_date": session,
                                     "elapsed_s": elapsed, "tickers_ok": len(results),
                                     "tickers_failed": len(failed), "failed_sample": dict(list(failed.items())[:25]),
                                     "sources": {**(prev.get("sources") or {}), **status.to_dict()},
                                     "runs": runs})
    log(f"Hecho en {elapsed}s: {summary}")
    return summary, status


def build_site(out_dir):
    """Ensambla la web estatica: docs/* + data/*.json -> out_dir (para GitHub Pages)."""
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    shutil.copytree(DOCS_DIR, out_dir)
    dd = os.path.join(out_dir, "data")
    os.makedirs(dd, exist_ok=True)
    for f in ("latest.json", "flow.json", "catalysts.json", "political.json", "futures.json", "status.json"):
        src = _out(f)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(dd, f))
    open(os.path.join(out_dir, ".nojekyll"), "w").close()
    log(f"Web generada en {out_dir}")
