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


def trade_idea(direction, m, b, next_cat):
    if direction not in ("ALCISTA", "BAJISTA"):
        return None
    price = m.get("price")
    iv_rank = b.get("iv_rank")
    expensive = (iv_rank is not None and iv_rank >= 60) or (m.get("front_premium") or 0) >= 1.25
    min_dte = 30
    if next_cat and next_cat.get("days") is not None and next_cat["days"] >= 0:
        min_dte = max(21, next_cat["days"] + 7)
    exp, dte = pick_expiration(m, min_dte)
    em = None
    for x in m.get("expected_moves") or []:
        if exp and x["expiration"] == exp:
            em = x["em_usd"]
    leg = "call" if direction == "ALCISTA" else "put"
    if price and em:
        k1 = round(price)
        k2 = round(price + em) if direction == "ALCISTA" else round(price - em)
        strikes = f"comprar {leg} ~{k1}, vender {leg} ~{k2}" if expensive else f"{leg} ~{k1} (ATM) o spread {k1}/{k2}"
    else:
        strikes = f"{leg} ATM"
    return {
        "structure": (f"{leg.capitalize()} debit spread" if expensive else f"{leg.capitalize()} larga o debit spread"),
        "why_structure": "IV cara: el spread abarata la prima y reduce el efecto del IV crush" if expensive
                         else "IV razonable: la opcion simple aprovecha mejor el movimiento",
        "expiration": exp, "dte": dte, "strikes": strikes,
        "management": "Stop -50% de la prima; objetivo +50/100%; salir si el OI de los contratos marcados empieza a caer",
        "note": "Ejemplo educativo, no es una recomendacion.",
    }


def score_ticker(m, b, fs, cats, cong, fut_bias, fut_why, enr, cfg):
    sc = cfg["scoring"]
    reasons = []
    comp = {}

    # 1) flujo inusual
    prev_bull, prev_bear = fs.get("prev_bull_premium", 0), fs.get("prev_bear_premium", 0)
    bull_all = (m.get("bull_premium") or 0) + 0.5 * prev_bull
    bear_all = (m.get("bear_premium") or 0) + 0.5 * prev_bear
    clarity = abs(bull_all - bear_all) / (bull_all + bear_all) if (bull_all + bear_all) else 0
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
            kp += 5
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
