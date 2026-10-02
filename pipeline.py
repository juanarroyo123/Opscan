"""Orquestador de OpScan.

Modos:
  full       cierre (todo: calendario, congreso, futuros, opciones, Yahoo, alertas)
  premarket  antes de apertura (todo; confirma por OI el flujo de ayer con el OI nuevo)
  intraday   solo opciones (reutiliza calendario/congreso/futuros ya generados)
  smoke      prueba rapida con pocos valores (para comprobar que todas las fuentes responden)
"""
import datetime as dt
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import (alerts, catalysts, congress, enrich, futures, options, paper, scoring, shorts, state,
               technicals, tracking, universe)
from .config import DOCS_DIR, data_dir, load_config
from .util import Status, iso_now, log, parse_date, read_json, rnd, today_et, write_json

YAHOO_TO_GICS = {
    "Technology": "Information Technology", "Healthcare": "Health Care",
    "Financial Services": "Financials", "Consumer Cyclical": "Consumer Discretionary",
    "Consumer Defensive": "Consumer Staples", "Basic Materials": "Materials",
}
FULL_MODES = ("full", "premarket", "smoke")


def pd_to_num(s):
    import pandas as pd
    return pd.to_numeric(s, errors="coerce")


def _out(name):
    return os.path.join(data_dir(), name)


def _finish(tk, und, contracts, ocfg, pending_syms, watch_syms, keep_grid):
    m, unusual = options.analyze_chain(und, contracts, ocfg)
    oi_map = {c["symbol"]: c["oi"] for c in contracts if c["symbol"] in pending_syms}
    m["stock_volume"] = und.get("stock_volume")
    m["iv30_change"] = und.get("iv30_change")
    out = {"ticker": tk, "metrics": m, "unusual": unusual, "oi_map": oi_map,
           "prices": options.price_map(contracts, watch_syms)}
    if keep_grid or unusual:
        out["grid"] = options.option_grid(contracts, und.get("price"), max_exps=6, width=0.35)
    if tk in options.GEX_TICKERS:
        try:
            out["gex"] = options.gex_profile(contracts, und.get("price"))
        except Exception as e:
            log(f"gex {tk}: {e}")
    return out


def scan_one(tk, ocfg, pending_syms, watch_syms=(), keep_grid=False):
    """Descarga y analiza un valor. Devuelve dict (sin la cadena completa para ahorrar memoria)."""
    t0 = time.time()
    und, contracts = options.parse_cboe(options.fetch_cboe(tk), tk)
    if not contracts:
        raise RuntimeError("Cboe sin contratos")
    out = _finish(tk, und, contracts, ocfg, pending_syms, set(watch_syms), keep_grid)
    out["secs"] = round(time.time() - t0, 2)
    return out


def scan_one_yf(tk, ocfg, pending_syms, watch_syms=(), keep_grid=False):
    und, contracts = options.fetch_yfinance(tk, ocfg["max_dte"])
    if not contracts:
        raise RuntimeError("Yahoo sin contratos")
    out = _finish(tk, und, contracts, ocfg, pending_syms, set(watch_syms), keep_grid)
    out["secs"] = 0
    return out


def _recent_flag_tickers(flags, days=10):
    """Valores con flujo marcado en los ultimos dias: guardamos su cadena para la idea con patas."""
    if not len(flags):
        return set()
    since = (today_et() - dt.timedelta(days=days)).isoformat()
    f = flags[(flags["date"] >= since) & flags["status"].isin(["PENDIENTE", "CONFIRMADA"])]
    return set(f["ticker"])


def scan_options(tickers, cfg, flags, status, allow_fallback=True, watch=None, keep_grid=()):
    ocfg = cfg["options"]
    watch = watch or {}
    keep_grid = set(keep_grid)
    pending = {}
    if len(flags):
        p = flags[flags["status"] == "PENDIENTE"]
        for tk, sym in zip(p["ticker"], p["symbol"]):
            pending.setdefault(tk, set()).add(sym)
    results, failed = {}, {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=int(ocfg["workers"])) as ex:
        futs = {ex.submit(scan_one, tk, ocfg, pending.get(tk, set()), watch.get(tk, set()), tk in keep_grid): tk
                for tk in tickers}
        for i, f in enumerate(as_completed(futs), 1):
            tk = futs[f]
            try:
                results[tk] = f.result()
            except Exception as e:
                failed[tk] = str(e)[:200]
            if i % 100 == 0:
                log(f"  opciones: {i}/{len(tickers)} ({time.time()-t0:.0f}s)")
    # segundo intento con Cboe (fallos puntuales de red): evita saltar a Yahoo, que estima peor el lado
    if failed:
        time.sleep(3)
        for tk in list(failed.keys()):
            try:
                results[tk] = scan_one(tk, ocfg, pending.get(tk, set()), watch.get(tk, set()), tk in keep_grid)
                failed.pop(tk, None)
            except Exception:
                pass
            time.sleep(0.3)
    status.ok("Cboe opciones", len(results), f"{len(failed)} fallos" if failed else "")
    # respaldo Yahoo
    fb = list(failed.keys())[: int(ocfg["yfinance_fallback_max"])] if allow_fallback else []
    ok_fb = 0
    for tk in fb:
        try:
            results[tk] = scan_one_yf(tk, ocfg, pending.get(tk, set()), watch.get(tk, set()), tk in keep_grid)
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


def tag_dividends(m, unusual, enr, session):
    """Calls muy dentro del dinero compradas justo antes del ex-dividendo = captura de dividendo
    (operacion de arbitraje), no una apuesta. Se marcan y casi no cuentan."""
    exd = (enr or {}).get("ex_div_date")
    if not exd or exd < session:
        return m, unusual, None
    hits = []
    out = []
    for u in unusual:
        u = dict(u)
        if (u["kind"] == "CALL" and (u.get("delta") or 0) >= 0.7 and u["expiration"] >= exd
                and (parse_date(exd) - parse_date(session)).days <= 45):
            u["dividend"] = True
            u["weight"] = min(u.get("weight", 1.0), 0.1)
            hits.append(u["symbol"])
        out.append(u)
    if hits:
        m = options.recompute_flow(dict(m), out)
        return m, out, {"ex_div_date": exd, "contracts": len(hits)}
    return m, unusual, None


def build_record(tk, res, meta, base, fs, cats, cong, fctx, enr, cfg, session=None, short_vol=None, tech=None):
    m = dict(res["metrics"])
    unusual = res["unusual"]
    m, unusual, div = tag_dividends(m, unusual, enr, session or m.get("session_date") or "")
    m["_unusual"] = unusual
    m["_grid"] = res.get("grid") or {}
    sector = _sector(meta, enr)
    fut_bias, fut_why = fctx.get(sector) or fctx.get("_default") or (None, [])
    s = scoring.score_ticker(m, base, fs, cats, cong, fut_bias, fut_why, enr, cfg, tech)
    e = enr or {}
    if div:
        s["reasons"].append(f"{div['contracts']} call(s) muy dentro del dinero antes del ex-dividendo "
                            f"({div['ex_div_date']}): captura de dividendo, no cuenta")
    # apreton de cortos
    sv = (short_vol or {}).get(tk) or {}
    sq = shorts.squeeze_risk(e.get("short_pct_float"), sv.get("ratio_5d"), s["direction"],
                             m.get("change_pct"), base.get("rel_vol"), m.get("cp_vol_ratio"))
    if sq and sq["level"] == "ALTO" and s["direction"] == "ALCISTA":
        s["score"] = round(min(100, s["score"] + 3), 1)
        s["components"]["cortos"] = 3
        s["reasons"].append("Riesgo de apreton de cortos ALTO (+3)")
        sc = cfg["scoring"]
        s["signal"] = "ALTA" if s["score"] >= sc["alta"] else ("MEDIA" if s["score"] >= sc["media"] else "BAJA")
    # resultados: movimiento esperado vs historico
    earn = None
    nxt = next((c for c in (cats or []) if c.get("type") == "Resultados" and (c.get("days") or -1) >= 0), None)
    if nxt and e.get("earn_hist"):
        earn = enrich.earnings_vs_expected(e["earn_hist"], m.get("expected_moves"), nxt["date"])
        if earn:
            earn["date"] = nxt["date"]
            earn["moves"] = e["earn_hist"].get("moves", [])[:8]
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
        "short": {"vol_ratio_5d": sv.get("ratio_5d"), "vol_ratio_last": sv.get("ratio_last"),
                  "pct_float": e.get("short_pct_float"), "days_to_cover": e.get("short_ratio")},
        "squeeze": sq, "earnings": earn, "tech": tech, "dividend": ({"ex_div_date": e.get("ex_div_date"),
                                                       "yield": e.get("dividend_yield")} if e.get("ex_div_date") else None),
        **s,
    }
    rec["unusual"] = unusual[:10]
    # registros sin interes: version compacta para que la web cargue rapido
    if (rec.get("score") or 0) < 20 and not unusual and "watchlist" not in rec["groups"]:
        for k in ("iv_term", "expected_moves", "news", "insiders", "confirmed", "catalysts"):
            rec.pop(k, None)
        rec["compact"] = True
    return rec


def sector_map(records):
    out = {}
    for r in records:
        if r.get("signal") == "ERR" or not r.get("sector") or r.get("sector") in ("ETF", "Indice"):
            continue
        s = out.setdefault(r["sector"], {"sector": r["sector"], "n": 0, "bull": 0.0, "bear": 0.0,
                                         "chg": [], "tickers": []})
        s["n"] += 1
        s["bull"] += r.get("bull_premium") or 0
        s["bear"] += r.get("bear_premium") or 0
        if r.get("change_pct") is not None:
            s["chg"].append(r["change_pct"])
        net = (r.get("bull_premium") or 0) - (r.get("bear_premium") or 0)
        if net:
            s["tickers"].append((net, r["ticker"]))
    res = []
    for s in out.values():
        tot = s["bull"] + s["bear"]
        s["tickers"].sort()
        res.append({"sector": s["sector"], "n": s["n"], "bull": round(s["bull"]), "bear": round(s["bear"]),
                    "net": round(s["bull"] - s["bear"]), "bias": round((s["bull"] - s["bear"]) / tot, 3) if tot else 0,
                    "avg_chg": round(sum(s["chg"]) / len(s["chg"]), 2) if s["chg"] else None,
                    "top_bull": [t for _, t in s["tickers"][::-1][:4] if _ > 0],
                    "top_bear": [t for _, t in s["tickers"][:4] if _ < 0]})
    return sorted(res, key=lambda x: -x["net"])


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

    daily = state.load_daily()
    low_act = set()
    if len(daily):
        import pandas as pd
        g = daily.sort_values("date").groupby("ticker")
        cnt = g.size()
        avg = g["opt_vol"].apply(lambda s: pd.to_numeric(s, errors="coerce").tail(20).mean())
        low_act = {t for t in avg.index if cnt.get(t, 0) >= 10 and (avg[t] or 0) < cfg["universe"]["min_avg_opt_vol"]}

    # 2) universo
    if tickers_override:
        uni = {t.upper(): {"name": t.upper(), "sector": "", "groups": ["manual"]} for t in tickers_override}
    elif mode == "smoke":
        base_u = universe.build_universe(cfg, None, None, offline=True)
        pick = (cfg["universe"]["watchlist"][:4] + ["SPY", "SPX", "AAPL", "NVDA"])
        uni = {t: base_u.get(t, {"name": t, "sector": "", "groups": ["smoke"]}) for t in pick}
    else:
        uni = universe.build_universe(cfg, status, cat_tickers, offline=offline_universe, low_activity=low_act)
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

    # 5) opciones (+ precios de la cartera simulada)
    flags = state.load_flags()
    paper.configure(cfg)
    book = paper.load()
    watch = paper.watch_symbols(book)
    for tk in watch:
        if tk not in uni:
            uni[tk] = {"name": tk, "sector": "", "groups": ["cartera"]}
    results, failed = scan_options(list(uni.keys()), cfg, flags, status, allow_fallback=full or bool(tickers_override),
                                   watch=watch, keep_grid=set(cfg["universe"]["watchlist"]) | _recent_flag_tickers(flags))
    sessions = [r["metrics"]["session_date"] for r in results.values() if r["metrics"].get("session_date")]
    session = max(set(sessions), key=sessions.count) if sessions else today_et().isoformat()
    for tk, res in results.items():
        sd = res["metrics"].get("session_date") or session
        flags = state.confirm_flags(flags, tk, res["oi_map"], sd, cfg["options"]["oi_confirm_ratio"])
        flags = state.add_flags(flags, res["unusual"], sd)
    flags = state.prune_flags(flags)
    daily = state.upsert_daily(daily, [r["metrics"] for r in results.values()])
    book = paper.mark(book, {tk: r.get("prices", {}) for tk, r in results.items()},
                      {tk: r["metrics"].get("price") for tk, r in results.items()}, session)

    # ventas en corto (FINRA): diario en modos completos, cache en intradia
    short_vol = state.load_cache("shorts.json", {})
    if full:
        try:
            sv = shorts.fetch_short_volume()
            short_vol = {t: v for t, v in sv.items() if t in uni}
            state.save_cache("shorts.json", short_vol)
            status.ok("FINRA (volumen en corto)", len(short_vol))
        except Exception as e:
            status.fail("FINRA (volumen en corto)", e)

    # 6) puntuacion preliminar -> enriquecer top N -> puntuacion final
    ecache = state.load_cache("enrich.json", {})
    bases, fsums = {}, {}
    for tk, res in results.items():
        m = res["metrics"]
        bases[tk] = state.baselines(daily, tk, m.get("session_date") or session, m.get("opt_vol"), m.get("iv30"))
        fsums[tk] = state.flag_summary(flags, tk, m.get("session_date") or session,
                                       cfg["options"]["oi_confirm_window"])
    # contexto tecnico de la accion (Yahoo en modos completos, cache en intradia)
    tcache = state.load_cache("tech.json", {})
    if full:
        stocks = [t for t in results if t not in options.INDEXES]
        tcache = technicals.build(stocks, tcache, status)
        state.save_cache("tech.json", tcache)
    techs = {tk: technicals.features(tk, tcache, daily, res["metrics"].get("stock_volume"))
             for tk, res in results.items()}

    prelim = []
    for tk, res in results.items():
        r = build_record(tk, res, uni.get(tk), bases[tk], fsums[tk], cal_by_tk.get(tk),
                         cong_by.get(tk), fctx, ecache.get(tk), cfg, session, short_vol, techs.get(tk))
        prelim.append((r["score"], tk))
    if full:
        prelim.sort(reverse=True)
        want = [tk for _, tk in prelim[: int(cfg["enrich"]["top_n"])]]
        want += [t for t in cfg["universe"]["watchlist"] if t in results and t not in want]
        # + valores con calls muy dentro del dinero (posible captura de dividendo)
        want += [tk for tk, res in results.items() if tk not in want and any(
            u["kind"] == "CALL" and (u.get("delta") or 0) >= 0.7 for u in res["unusual"])][:40]
        want = [t for t in want if t not in options.INDEXES
                and not ({"etf", "indice"} & set((uni.get(t) or {}).get("groups", [])))]
        ecache = enrich.enrich(want, ecache, n_news=int(cfg["enrich"]["news"]), status=status)
        # historico de reacciones a resultados para los que presentan en <= 30 dias
        soon = [tk for tk in want if any(c.get("type") == "Resultados" and 0 <= (c.get("days") or -1) <= 30
                                         for c in cal_by_tk.get(tk, []))
                or (lambda d: d is not None and 0 <= (d - today_et()).days <= 30)(
                    parse_date((ecache.get(tk) or {}).get("earnings_date")))]
        ecache = enrich.enrich_earnings(soon, ecache, status=status)
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
                                    cong_by.get(tk), fctx, ecache.get(tk), cfg, session, short_vol, techs.get(tk)))
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
        cf = flags[flags["status"] == "CONFIRMADA"]
        if "weight" in cf.columns:
            w = pd_to_num(cf["weight"]).fillna(1.0)
            cf = cf[w >= 0.5]
        cf = cf.sort_values("checked_date", ascending=False).head(300)
        for _, r in cf.iterrows():
            confirmed_recent.append({k: (None if str(r[k]) == "nan" else r[k]) for k in
                                     ("date", "checked_date", "ticker", "symbol", "kind", "strike", "expiration",
                                      "volume", "oi_before", "oi_after", "premium", "direction", "side")})

    # 8) cartera simulada (AUTO abre cada ENTRADA), registro de aciertos, GEX y sectores
    ok_recs = [r for r in records if r.get("signal") != "ERR"]
    n_auto = paper.auto_open(book, ok_recs, session)
    paper.snapshot(book, session)
    paper.save(book)
    paper_out = paper.export(book)
    status.ok("Cartera simulada", len(book["trades"]), f"{n_auto} nuevas AUTO" if n_auto else "")
    signals = tracking.load_signals()
    if mode in ("full", "premarket", "smoke"):
        signals = tracking.record_signals(signals, ok_recs, session)
        tracking.save_signals(signals)
    track = tracking.build(signals, daily)
    market_gex = {tk: res["gex"] for tk, res in results.items() if res.get("gex")}
    sectors_out = sector_map(ok_recs)

    # 9) alertas
    sent = state.load_cache("alerts.json", {})
    sent = alerts.process(ok_recs, cfg, sent, session, status)
    if mode == "full":
        conf_today = [c for c in confirmed_recent if c.get("checked_date") == session]
        sent = alerts.daily_summary(ok_recs, {
            "entradas": sum(1 for r in ok_recs if r.get("checklist", {}).get("entrada")),
            "alta": sum(1 for r in ok_recs if r["signal"] == "ALTA"),
            "media": sum(1 for r in ok_recs if r["signal"] == "MEDIA"),
            "pre_alertas": sum(1 for r in ok_recs if r.get("checklist", {}).get("pre_alerta"))},
            session, sent, paper_out["summary"], conf_today, market_gex.get("SPX") or market_gex.get("SPY"))
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
    write_json(_out("tracking.json"), track)
    write_json(_out("paper.json"), paper_out)
    write_json(_out("latest.json"), {"generated_at": gen, "mode": mode, "session_date": session,
                                     "repo": os.environ.get("GITHUB_REPOSITORY", ""),
                                     "market_gex": market_gex, "sectors": sectors_out,
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
                                     "sources": status.to_dict() if full else {**(prev.get("sources") or {}), **status.to_dict()},
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
    for f in ("latest.json", "flow.json", "catalysts.json", "political.json", "futures.json", "status.json",
              "tracking.json", "paper.json"):
        src = _out(f)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(dd, f))
    open(os.path.join(out_dir, ".nojekyll"), "w").close()
    log(f"Web generada en {out_dir}")
