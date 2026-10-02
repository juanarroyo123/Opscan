"""Puntuacion de confluencia por valor (0-100), direccion y checklist de la estrategia.

Pilares:
  Flujo inusual (0-30) | Volumen relativo (0-10) | Confirmacion por OI (0-20)
  Catalizador (0-15)   | Congreso (0-15)        | Contexto de futuros (0-10)

Checklist de entrada (estrategia del proyecto):
  congreso a favor (+1) + futuros a favor (+1) + flujo confirmado por OI (+2)
  ENTRADA si el flujo esta confirmado y la suma >= entry_min_points (3 por defecto).
"""
import math

from .util import parse_date, rnd

STRONG_CATALYSTS = ("PDUFA", "AdCom", "Lectura", "Resultados", "Ensayo", "Manual", "Datos")


def _sign(x, thr=0.0):
    if x is None:
        return 0
    return 1 if x > thr else (-1 if x < -thr else 0)


def flow_points(m, prev_premium=0, clarity=None):
    """0-30. Combina tamano (prima inusual de hoy + mitad de la previa vigente), concentracion
    (que parte de TODA la prima del valor es inusual: en SPY/NVDA el flujo inusual es una gota)
    y claridad direccional (todo calls compradas = 1; mezcla = 0)."""
    today = m.get("effective_premium", m.get("unusual_premium")) or 0
    up = today + 0.5 * (prev_premium or 0)
    if up <= 0:
        return 0.0
    total = (m.get("call_premium") or 0) + (m.get("put_premium") or 0)
    share = up / total if total else 1
    size = min(1.0, math.log10(1 + up / 50000) / 1.5)
    conc = math.sqrt(min(1.0, share / 0.3))
    clar = abs(m.get("flow_bias") or 0) if clarity is None else clarity
    pts = 30 * size * conc * (0.4 + 0.6 * clar) + min(m.get("whales") or 0, 3)
    return round(min(30.0, pts), 1)


def pick_expiration(m, min_dte):
    for t in m.get("iv_term") or []:
        if t["dte"] >= min_dte:
            return t["expiration"], t["dte"]
    for t in m.get("expected_moves") or []:
        if t["dte"] >= min_dte:
            return t["expiration"], t["dte"]
    return None, None


def build_legs(direction, grid, exp, price, em, spread):
    """Elige contratos REALES de la rejilla: compra ~ATM y, si es spread, vende ~precio +/- mov. esperado."""
    if not grid or exp not in grid or not price:
        return None
    kind = "C" if direction == "ALCISTA" else "P"
    cs = [c for c in grid[exp] if c["kind"] == kind]
    if not cs:
        return None

    def spr(c):
        return (c["ask"] - c["bid"]) / c["mid"] if c.get("mid") and c["ask"] > 0 and c["bid"] > 0 else 9.9

    # preferir contratos liquidos (diferencia compra/venta <= 15% del precio) cerca del dinero
    liquid = [c for c in cs if spr(c) <= 0.15 and abs(c["strike"] / price - 1) <= 0.08]
    buy = min(liquid or cs, key=lambda c: abs(c["strike"] - price))
    legs = [{"action": "COMPRAR", **buy}]
    sell = None
    if spread and em:
        target = price + em if kind == "C" else price - em
        cands = [c for c in cs if (c["strike"] > buy["strike"] if kind == "C" else c["strike"] < buy["strike"])]
        if cands:
            liq = [c for c in cands if spr(c) <= 0.25]
            sell = min(liq or cands, key=lambda c: abs(c["strike"] - target))
            legs.append({"action": "VENDER", **sell})
    debit = buy["mid"] - (sell["mid"] if sell else 0)
    if debit <= 0:
        return None
    out = {"legs": [{**{k: l.get(k) for k in ("action", "symbol", "kind", "strike", "expiration", "dte", "mid",
                                              "bid", "ask", "delta", "iv")},
                     "spread_pct": round(spr(l) * 100, 1) if spr(l) < 9 else None} for l in legs],
           "cost": round(debit * 100, 2), "max_loss": round(debit * 100, 2)}
    worst = max(spr(l) for l in legs)
    out["liquidity"] = "buena" if worst <= 0.08 else ("aceptable" if worst <= 0.15 else "mala")
    if out["liquidity"] == "mala":
        out["liquidity_note"] = (f"Diferencia compra/venta de hasta {worst*100:.0f}%: entrarias perdiendo. "
                                 "Usa ordenes limitadas al precio medio o evita la operacion.")
    if sell:
        width = abs(sell["strike"] - buy["strike"])
        out["max_gain"] = round((width - debit) * 100, 2)
        out["reward_risk"] = round((width - debit) / debit, 2) if debit else None
    else:
        out["max_gain"] = None   # ilimitada (call) / hasta strike (put)
    out["breakeven"] = round(buy["strike"] + debit, 2) if kind == "C" else round(buy["strike"] - debit, 2)
    out["breakeven_move_pct"] = round((out["breakeven"] / price - 1) * 100, 1)
    return out


def trade_idea(direction, m, b, next_cat):
    if direction not in ("ALCISTA", "BAJISTA"):
        return None
    price = m.get("price")
    iv_rank = b.get("iv_rank")
    expensive = (iv_rank is not None and iv_rank >= 60) or (m.get("front_premium") or 0) >= 1.25 \
        or (m.get("iv30") or 0) >= 0.6
    min_dte = 30
    if next_cat and next_cat.get("days") is not None and next_cat["days"] >= 0:
        min_dte = max(21, next_cat["days"] + 7)
    grid = m.get("_grid") or {}
    exp, dte = None, None
    for e in sorted(grid.keys()):
        d = grid[e][0]["dte"] if grid[e] else 0
        if d >= min_dte:
            exp, dte = e, d
            break
    if not exp:
        exp, dte = pick_expiration(m, min_dte)
    em = None
    for x in m.get("expected_moves") or []:
        if exp and x["expiration"] == exp:
            em = x["em_usd"]
    if em is None and price and m.get("iv30") and dte:
        em = round(price * m["iv30"] * math.sqrt(dte / 365), 2)
    leg = "call" if direction == "ALCISTA" else "put"
    if price and em:
        k1 = round(price)
        k2 = round(price + em) if direction == "ALCISTA" else round(price - em)
        strikes = f"comprar {leg} ~{k1}, vender {leg} ~{k2}" if expensive else f"{leg} ~{k1} (ATM) o spread {k1}/{k2}"
    else:
        strikes = f"{leg} ATM"
    idea = {
        "structure": (f"{leg.capitalize()} debit spread" if expensive else f"{leg.capitalize()} larga"),
        "why_structure": "IV cara: el spread abarata la prima y reduce el efecto del IV crush" if expensive
                         else "IV razonable: la opcion simple aprovecha mejor el movimiento",
        "expiration": exp, "dte": dte, "strikes": strikes,
        "management": "Stop -50% de la prima; objetivo +50/100%; salir si el OI de los contratos marcados empieza a caer",
        "note": "Ejemplo educativo, no es una recomendacion.",
    }
    legs = build_legs(direction, grid, exp, price, em, spread=expensive)
    if legs:
        idea.update(legs)
        k = [l["strike"] for l in legs["legs"]]
        idea["strikes"] = " / ".join(f"{l['action'].lower()} {leg} {l['strike']:g} a ${l['mid']}" for l in legs["legs"])
    return idea


def score_ticker(m, b, fs, cats, cong, fut_bias, fut_why, enr, cfg, tech=None):
    sc = cfg["scoring"]
    reasons = []
    comp = {}

    # 1) flujo inusual
    prev_bull, prev_bear = fs.get("prev_bull_premium", 0), fs.get("prev_bear_premium", 0)
    bull_all = (m.get("bull_premium") or 0) + 0.5 * prev_bull
    bear_all = (m.get("bear_premium") or 0) + 0.5 * prev_bear
    # claridad sobre TODA la prima efectiva (lo indeterminado/MID diluye la direccion)
    eff_all = max((m.get("effective_premium") or 0) + 0.5 * (prev_bull + prev_bear), bull_all + bear_all)
    clarity = abs(bull_all - bear_all) / eff_all if eff_all else 0
    fp = flow_points(m, prev_bull + prev_bear, clarity)
    comp["flujo"] = round(fp, 1)
    if m.get("unusual_count"):
        reasons.append(f"Hoy: {m['unusual_count']} contratos inusuales, ${m['unusual_premium']/1e6:.2f}M de prima")
    if prev_bull + prev_bear > 0:
        reasons.append(f"Sesiones previas: ${(prev_bull + prev_bear)/1e6:.2f}M en flujo inusual vigente")
    if fp > 0:
        reasons.append(f"Puntos de flujo (+{fp:.0f})")
        if m.get("whales"):
            reasons.append(f"{m['whales']} orden(es) ballena >= $1M")

    # 2) volumen relativo
    rv = b.get("rel_vol")
    rp = 10 if rv and rv >= 3 else 6 if rv and rv >= 2 else 3 if rv and rv >= 1.5 else 0
    comp["vol_relativo"] = rp
    if rp:
        reasons.append(f"Volumen de opciones {rv}x su media 20d (+{rp})")

    # 3) confirmacion OI
    cp = 0
    if fs["confirmed"]:
        cp = 8 if fs["confirmed"] < 3 else 14
        if fs["confirmed_bull_premium"] + fs["confirmed_bear_premium"] >= 1_000_000:
            cp += 6
        cp = min(20, cp)
        reasons.append(f"{fs['confirmed']} alerta(s) confirmadas por subida de OI (+{cp})")
    comp["oi_confirmado"] = cp

    # direccion
    conf_tot = fs["confirmed_bull_premium"] + fs["confirmed_bear_premium"]
    conf_bias = (fs["confirmed_bull_premium"] - fs["confirmed_bear_premium"]) / conf_tot if conf_tot else 0
    cong_bias = (cong or {}).get("bias") or 0
    ins = (enr or {}).get("insiders") or {}
    ins_tot = (ins.get("buy_usd") or 0) + (ins.get("sell_usd") or 0)
    ins_bias = ((ins.get("buy_usd") or 0) - (ins.get("sell_usd") or 0)) / ins_tot if ins_tot else 0
    prev_tot = prev_bull + prev_bear
    prev_bias = (prev_bull - prev_bear) / prev_tot if prev_tot else 0
    d = (m.get("flow_bias") or 0) * 1.0 + prev_bias * 0.6 + conf_bias * 1.2 + cong_bias * 0.5 + ins_bias * 0.2
    direction = "ALCISTA" if d > 0.15 else ("BAJISTA" if d < -0.15 else "MIXTO")
    dsign = 1 if direction == "ALCISTA" else (-1 if direction == "BAJISTA" else 0)

    # 1b) flujo reactivo: si la accion YA se movio mucho hoy en esa direccion, las opciones
    #     probablemente persiguen la noticia (llegan tarde) -> vale menos que el flujo anticipado
    chg = m.get("change_pct")
    if dsign and chg is not None and _sign(chg) == dsign and comp["flujo"] > 0:
        if abs(chg) >= 6:
            comp["flujo"] = round(comp["flujo"] * 0.6, 1)
            reasons.append(f"Flujo reactivo: la accion ya se movio {chg:+.1f}% hoy (flujo x0,6)")
        elif abs(chg) >= 3.5:
            comp["flujo"] = round(comp["flujo"] * 0.85, 1)
            reasons.append(f"Flujo en parte reactivo: accion {chg:+.1f}% hoy (flujo x0,85)")
    elif dsign and chg is not None and _sign(chg, 1.0) == -dsign and comp["flujo"] > 0:
        reasons.append(f"Flujo anticipado/contrario: se apuesta en contra del movimiento de hoy ({chg:+.1f}%)")

    # 1c) acumulacion: flujo en la misma direccion varias sesiones seguidas
    acc = 0
    if dsign:
        ns = fs.get("sessions_bull", 0) if dsign > 0 else fs.get("sessions_bear", 0)
        if ns >= 3:
            acc = 7
        elif ns == 2:
            acc = 4
        if acc:
            reasons.append(f"Acumulacion: flujo {direction.lower()} en {ns} sesiones distintas (+{acc})")
    comp["acumulacion"] = acc

    # 1d) confirmacion de la accion: tendencia y volumen
    ap = 0
    t = tech or {}
    if dsign and t:
        if t.get("above20") is not None and t.get("above50") is not None:
            if (dsign > 0 and t["above20"] and t["above50"]) or (dsign < 0 and not t["above20"] and not t["above50"]):
                ap += 3
                reasons.append("Tendencia a favor (precio " + ("sobre" if dsign > 0 else "bajo") + " sus medias de 20 y 50 dias) (+3)")
            elif (dsign > 0 and not t["above50"]) or (dsign < 0 and t["above50"]):
                reasons.append("Contra tendencia (la media de 50 dias va en contra)")
        rsv = t.get("rel_stock_vol")
        if rsv is not None and rsv >= 2 and chg is not None and _sign(chg) == dsign:
            ap += 3
            reasons.append(f"La accion confirma: volumen {rsv}x su media con precio {chg:+.1f}% (+3)")
    comp["accion"] = ap

    # 1e) volatilidad implicita subiendo = alguien paga por la prisa
    ivp = 0
    ivc = m.get("iv30_change")
    if dsign and ivc is not None and abs(m.get("flow_bias") or 0) >= 0.3:
        if ivc >= 0.02:
            ivp = 3
            reasons.append(f"IV30 sube {ivc*100:+.1f} pts con flujo direccional: compradores con prisa (+3)")
        elif ivc <= -0.03:
            reasons.append(f"IV30 baja {ivc*100:+.1f} pts: el flujo no presiona la volatilidad")
    comp["iv_subiendo"] = ivp

    # 4) catalizador
    horizon = sc["catalyst_horizon_days"]
    upcoming = [c for c in (cats or []) if c.get("days") is not None and 0 <= c["days"] <= horizon]

    def cat_base(c):
        strong = any(k in c["type"] for k in STRONG_CATALYSTS)
        factor = 0.6 if c["type"] == "Resultados" else (1 if strong else 0.5)   # resultados: evento conocido por todos
        # PDUFA/AdCom/lecturas: binarios, cuentan casi enteros aunque falten semanas
        decay = (1 - c["days"] / horizon) if c["type"] == "Resultados" or not strong else max(0.6, 1 - c["days"] / horizon)
        return 10 * decay * factor

    nc = max(upcoming, key=cat_base) if upcoming else None   # el catalizador mas relevante, no solo el mas cercano
    kp = 0.0
    if nc:
        kp = cat_base(nc)
        cd = parse_date(nc["date"])
        positioned = [u for u in list(m.get("_unusual", [])) + list(fs.get("confirmed_list") or [])
                      if parse_date(u["expiration"]) and cd and parse_date(u["expiration"]) >= cd]
        if positioned:
            kp += 2 if nc["type"] == "Resultados" else 5   # casi todo vence despues de unos resultados
            reasons.append(f"{len(positioned)} contrato(s) inusual(es) vencen despues del catalizador")
        kp = min(15, kp)
        reasons.append(f"{nc['type']} en {nc['days']}d ({nc['date']}) (+{kp:.0f})")
    comp["catalizador"] = round(kp, 1)

    # 5) congreso
    gp = 0
    if cong:
        nb, ns = cong.get("n_buyers", 0), cong.get("n_sellers", 0)
        cb = _sign(cong.get("bias"), 0.3)
        win = sc["congress_window_days"]
        if dsign and cb == dsign:
            n = nb if dsign > 0 else ns
            if dsign < 0 and n < 2:
                n = 0          # una venta aislada no dice nada (se vende por mil motivos)
            if n:
                gp = 9 if n >= 2 else 5
                if dsign > 0 and cong.get("cluster"):
                    gp += 3
                if dsign > 0 and cong.get("committee_relevant"):
                    gp += 3
                gp = min(15, gp)
                verb = "compraron" if dsign > 0 else "vendieron"
                extra = " (comite relevante)" if dsign > 0 and cong.get("committee_relevant") else ""
                reasons.append(f"{n} congresista(s) {verb} en {win}d{extra}: a favor (+{gp})")
        elif dsign and cb == -dsign:
            reasons.append(f"Congreso en contra: {nb} compradores / {ns} vendedores en {win}d")
        elif nb or ns:
            reasons.append(f"Congreso sin sesgo claro: {nb} compradores / {ns} vendedores en {win}d")
    comp["congreso"] = gp

    # 6) futuros
    fpnt = 0
    if fut_bias is not None and dsign:
        if _sign(fut_bias, 0.15) == dsign:
            fpnt = round(min(10, abs(fut_bias) * 10 + 3))
            reasons.append(f"Futuros a favor: {', '.join(fut_why)} (+{fpnt})")
        elif _sign(fut_bias, 0.15) == -dsign:
            reasons.append(f"Futuros en contra: {', '.join(fut_why)}")
    comp["futuros"] = fpnt

    total = round(min(100, sum(comp.values())), 1)
    label = "ALTA" if total >= sc["alta"] else ("MEDIA" if total >= sc["media"] else "BAJA")

    # checklist de la estrategia
    ck_cong = bool(cong) and dsign != 0 and _sign(cong.get("bias"), 0.3) == dsign and (
        cong.get("n_buyers") if dsign > 0 else cong.get("n_sellers"))
    ck_fut = fut_bias is not None and dsign != 0 and _sign(fut_bias, 0.2) == dsign
    conf_dir = fs["confirmed_bull_premium"] if dsign > 0 else fs["confirmed_bear_premium"] if dsign < 0 else 0
    ck_flow = conf_dir > 0
    pts = (1 if ck_cong else 0) + (1 if ck_fut else 0) + (2 if ck_flow else 0)
    entry = bool(ck_flow and pts >= sc["entry_min_points"] and total >= sc["media"])
    pre_alert = bool(not ck_flow and (m.get("unusual_count") or prev_tot) and dsign != 0 and
                     (1 if ck_cong else 0) + (1 if ck_fut else 0) >= 1)

    if b.get("iv_rank") is not None:
        reasons.append(f"IV rank {b['iv_rank']:.0f} (IV30 {m['iv30']*100:.0f}%)" if m.get("iv30") else f"IV rank {b['iv_rank']:.0f}")
    if (m.get("front_premium") or 0) >= 1.25:
        reasons.append(f"IV del vencimiento cercano {m['front_premium']}x la IV30: evento descontado")

    return {
        "score": total, "signal": label, "direction": direction, "direction_strength": rnd(d, 2),
        "components": comp, "reasons": reasons,
        "checklist": {"congreso": bool(ck_cong), "futuros": bool(ck_fut), "flujo_confirmado": ck_flow,
                      "puntos": pts, "entrada": entry, "pre_alerta": pre_alert},
        "next_catalyst": nc, "idea": trade_idea(direction, m, b, nc) if (entry or label != "BAJA") else None,
    }
