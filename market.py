"""Contexto de mercado y novedades del dia.

- semaforo(): VIX, ratio puts/calls del mercado y gamma del SPX -> verde / ambar / rojo.
- split_score(): separa la parte CONFIRMADA de la puntuacion (fiable) de la PROVISIONAL
  (flujo de hoy, que no se sabe si es apertura hasta que se publique el OI manana).
- changes(): que ha cambiado respecto a la sesion anterior (entradas nuevas/perdidas,
  senales confirmadas por OI, cambios de direccion).
- weekly_report(): informe de los lunes por Telegram.
"""
import datetime as dt

from .util import parse_date, rnd


def semaforo(records, gex=None, regime=None):
    by = {r["ticker"]: r for r in records}
    vix = by.get("VIX") or {}
    v, vchg = vix.get("price"), vix.get("change_pct")
    calls = puts = 0
    for r in records:
        if r.get("ticker") in ("VIX", "SPX", "NDX", "RUT", "XSP", "DJX"):
            continue
        calls += r.get("call_vol") or 0
        puts += r.get("put_vol") or 0
    pc = rnd(puts / calls, 2) if calls else None
    g = (gex or {}).get("regime")
    why, level = [], "verde"
    if v is not None:
        if v >= 25 or (vchg or 0) >= 15:
            level = "rojo"
            why.append(f"VIX {v:.1f} ({vchg:+.1f}% hoy): miedo alto, opciones muy caras")
        elif v >= 18 or (vchg or 0) >= 8:
            level = "ambar"
            why.append(f"VIX {v:.1f}: algo de nerviosismo")
        else:
            why.append(f"VIX {v:.1f}: mercado tranquilo")
    if g == "negativa":
        level = "ambar" if level == "verde" else level
        why.append("Gamma del S&P 500 negativa: los movimientos se amplifican")
    elif g == "positiva":
        why.append("Gamma positiva: los creadores de mercado frenan los movimientos")
    if pc is not None:
        if pc >= 1.1:
            level = "ambar" if level == "verde" else level
            why.append(f"Ratio puts/calls {pc}: mucha cobertura o apuestas bajistas")
        elif pc <= 0.6:
            why.append(f"Ratio puts/calls {pc}: mucho optimismo (ojo a la euforia)")
        else:
            why.append(f"Ratio puts/calls {pc}: normal")
    rb = (regime or {}).get("bias")
    if rb is not None and rb <= -0.4:
        level = "ambar" if level == "verde" else level
        why.append(f"Futuros (posicionamiento de grandes inversores) con sesgo bajista ({rb:+.2f})")
    elif rb is not None and rb >= 0.4:
        why.append(f"Futuros con sesgo alcista ({rb:+.2f})")
    advice = {"verde": "Condiciones normales para comprar opciones.",
              "ambar": "Sé más selectivo: compra menos contratos o espera confirmaciones.",
              "rojo": "Opciones caras y mercado nervioso: mejor esperar o arriesgar muy poco."}[level]
    return {"level": level, "vix": v, "vix_chg": vchg, "pc_ratio": pc, "gamma": g, "why": why, "advice": advice}


def split_score(rec, prev_bull, prev_bear, m):
    """Parte provisional = puntos de flujo que vienen del flujo de HOY (aun sin confirmar)."""
    comp = rec.get("components") or {}
    today = (m.get("bull_premium") or 0) + (m.get("bear_premium") or 0)
    prev = 0.5 * ((prev_bull or 0) + (prev_bear or 0))
    share = today / (today + prev) if (today + prev) else 0
    prov = round((comp.get("flujo") or 0) * share, 1)
    return {"confirmed": round(max(0, (rec.get("score") or 0) - prov), 1), "provisional": prov}


def snapshot(records):
    return {r["ticker"]: {"score": r.get("score"), "signal": r.get("signal"), "direction": r.get("direction"),
                          "entrada": bool((r.get("checklist") or {}).get("entrada")),
                          "conf": bool((r.get("checklist") or {}).get("flujo_confirmado"))}
            for r in records if r.get("signal") not in (None, "ERR")}


def roll(cache, session, records):
    """Guarda la foto de cada sesion; devuelve la de la sesion ANTERIOR."""
    cache = dict(cache or {})
    if cache.get("cur_session") and cache["cur_session"] != session:
        cache["prev_session"], cache["prev"] = cache["cur_session"], cache.get("cur") or {}
    cache["cur_session"], cache["cur"] = session, snapshot(records)
    return cache


def changes(cache):
    prev, cur = cache.get("prev") or {}, cache.get("cur") or {}
    if not prev:
        return {"since": None, "new_entries": [], "lost_entries": [], "confirmed": [], "flips": [], "new_alta": []}
    out = {"since": cache.get("prev_session"), "new_entries": [], "lost_entries": [], "confirmed": [],
           "flips": [], "new_alta": []}
    for tk, c in cur.items():
        p = prev.get(tk) or {}
        if c["entrada"] and not p.get("entrada"):
            out["new_entries"].append({"ticker": tk, "direction": c["direction"], "score": c["score"]})
        if c["conf"] and not p.get("conf"):
            out["confirmed"].append({"ticker": tk, "direction": c["direction"], "score": c["score"]})
        if c["signal"] == "ALTA" and p.get("signal") != "ALTA":
            out["new_alta"].append({"ticker": tk, "direction": c["direction"], "score": c["score"]})
        if p.get("direction") in ("ALCISTA", "BAJISTA") and c["direction"] in ("ALCISTA", "BAJISTA") \
                and p["direction"] != c["direction"] and (c["score"] or 0) >= 30:
            out["flips"].append({"ticker": tk, "from": p["direction"], "to": c["direction"], "score": c["score"]})
    for tk, p in prev.items():
        if p.get("entrada") and not (cur.get(tk) or {}).get("entrada"):
            out["lost_entries"].append({"ticker": tk, "direction": p["direction"],
                                        "score": (cur.get(tk) or {}).get("score")})
    for k in ("new_entries", "confirmed", "new_alta", "flips", "lost_entries"):
        out[k] = sorted(out[k], key=lambda x: -(x.get("score") or 0))[:15]
    return out


def _week_change(curve, today):
    if not curve:
        return None, None
    since = (today - dt.timedelta(days=7)).isoformat()
    old = [x for x in curve if x["date"] <= since] or curve[:1]
    a, b = old[-1]["equity"], curve[-1]["equity"]
    return b, (b / a - 1) * 100 if a else None


def weekly_report(paper_out, records, today=None, site=""):
    today = today or dt.date.today()
    curves = paper_out.get("curves") or {}
    acc = (paper_out.get("summary") or {}).get("account") or {}
    eq_m, wk_m = _week_change(curves.get("MANUAL"), today)
    eq_a, wk_a = _week_change(curves.get("AUTO"), today)
    spy = next((r for r in records if r.get("ticker") == "SPY"), {})
    spy_wk = (spy.get("tech") or {}).get("chg_5d")
    lines = [f"OpScan · informe semanal ({today.isoformat()})", ""]
    if eq_m is not None:
        lines.append(f"Tu cartera: ${eq_m:,.0f} ({acc.get('return_pct', 0):+.2f}% total"
                     + (f", {wk_m:+.2f}% esta semana)" if wk_m is not None else ")"))
    if eq_a is not None:
        lines.append(f"Estrategia automatica: ${eq_a:,.0f}" + (f" ({wk_a:+.2f}% semana)" if wk_a is not None else ""))
    if spy_wk is not None:
        lines.append(f"S&P 500 (SPY) ultima semana: {spy_wk:+.2f}%")
    opens = [t for t in paper_out.get("trades", []) if t["status"] == "OPEN" and t["source"] == "MANUAL"]
    if opens:
        lines += ["", "Tus posiciones:"]
        for t in opens:
            adv = (t.get("advice") or {}).get("action", "")
            lines.append(f"- {t['ticker']} {t.get('pnl_pct', 0):+.0f}% {adv}")
        ev = []
        for t in opens:
            c = t.get("catalyst") or {}
            d = parse_date(c.get("date"))
            if d and 0 <= (d - today).days <= 7:
                ev.append(f"- {t['ticker']}: {c.get('type')} el {c['date']}")
            e = parse_date(t.get("expiration"))
            if e and 0 <= (e - today).days <= 7:
                ev.append(f"- {t['ticker']}: vence el {t['expiration']}")
        if ev:
            lines += ["", "Esta semana:"] + ev
    closed = [t for t in paper_out.get("trades", []) if t["status"] != "OPEN" and t["source"] == "MANUAL"
              and (t.get("closed") or "") >= (today - dt.timedelta(days=7)).isoformat()]
    if closed:
        lines += ["", "Cerradas esta semana:"] + [f"- {t['ticker']} {t.get('pnl_pct', 0):+.0f}%"
                                                  + (f" · aprendido: {t['lesson']}" if t.get("lesson") else "")
                                                  for t in closed]
    if site:
        lines += ["", site]
    lines.append("(cartera simulada, no es asesoramiento)")
    return "\n".join(lines)
