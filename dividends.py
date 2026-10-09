"""Dividendos y opciones compradas.

El dia del ex-dividendo la accion baja (mas o menos) lo que paga de dividendo. El precio de la
opcion en el mercado ya lo descuenta, pero nuestras cuentas (probabilidad de ganar, punto de
equilibrio) no lo hacian. Aqui:

- info(): proximo ex-dividendo (el anunciado o, si no, estimado con la frecuencia historica)
  y cuanto paga por accion.
- adjust_idea(): si el ex-dividendo cae ANTES del vencimiento, recalcula la probabilidad de ganar
  de cada opcion con la accion "sin dividendo" (baja para calls, sube para puts) y anade una nota.
- El Robot no compra una CALL si el dividendo que cae dentro de su vida es >= 30 % de lo que
  cuesta la opcion (config paper.max_div_share): demasiado lastre.
"""
import datetime as dt

from .util import parse_date


def _yield_frac(y):
    try:
        y = float(y)
    except (TypeError, ValueError):
        return None
    return y / 100 if y > 1 else y          # Yahoo a veces da 2.44 (%) y a veces 0.0244


def info(enr, price, today, history=()):
    """enr: datos de enrich (ex_div_date, dividend_rate, dividend_yield). history: ex-dividendos vistos."""
    if not enr:
        return None
    last = parse_date(enr.get("ex_div_date"))
    if not last:
        return None
    seen = sorted({d for d in (parse_date(x) for x in (history or ())) if d} | {last})
    gaps = [(b - a).days for a, b in zip(seen, seen[1:]) if 20 <= (b - a).days <= 400]
    step = sorted(gaps)[len(gaps) // 2] if gaps else 91
    freq = max(1, round(365 / step))
    rate = enr.get("dividend_rate")
    try:
        rate = float(rate) if rate else None
    except (TypeError, ValueError):
        rate = None
    if not rate:
        y = _yield_frac(enr.get("dividend_yield"))
        rate = y * price if (y and price) else None
    if not rate:
        return None
    nxt, est = last, False
    while nxt < today:                       # Yahoo suele dar el ultimo ya pasado: se estima el siguiente
        nxt = nxt + dt.timedelta(days=step)
        est = True
    amount = rate / freq
    return {"ex_div_date": last.isoformat(), "next_ex": nxt.isoformat(), "estimated": est,
            "amount": round(amount, 4), "rate": round(rate, 4), "freq": freq,
            "yield_pct": round(rate / price * 100, 2) if price else None}


def in_life(div, expiration):
    e = parse_date(expiration)
    n = parse_date((div or {}).get("next_ex"))
    return bool(div and e and n and n <= e)


def adjust_idea(idea, div, price, today, prob_fn):
    """Corrige la probabilidad de ganar por el dividendo y anota cuanto pesa sobre la prima."""
    if not idea or not div or not price:
        return idea
    amt = div["amount"]
    hit = False
    for c in (idea.get("choices") or []):
        if not in_life(div, c.get("expiration")):
            continue
        hit = True
        be = c.get("breakeven")
        days = c.get("dte") or (parse_date(c.get("expiration")) - today).days
        p = prob_fn(c["kind"], price - amt, be, c.get("iv"), days) if be else None
        if p is not None:
            c["pop_sin_div"] = c.get("pop")
            c["pop"] = p
        c["div_share"] = round(amt / c["mid"], 2) if c.get("mid") else None
    legs = (idea.get("legs") or [])
    main = legs[0] if legs else None
    if main and in_life(div, main.get("expiration")):
        hit = True
        idea["dividend"] = {**div, "share_of_premium": round(amt / main["mid"], 2) if main.get("mid") else None,
                            "kind": main.get("kind")}
    if hit:
        cuando = ("estimado ~" if div["estimated"] else "") + div["next_ex"]
        efecto = ("juega EN CONTRA de las calls" if (main or {}).get("kind", "C") == "C"
                  else "juega A FAVOR de las puts")
        idea["dividend_note"] = (f"Ex-dividendo {cuando}, antes del vencimiento: ese día la acción baja ~${amt:.2f} "
                                 f"(lo que paga). El precio de la opción ya lo descuenta, pero {efecto}; "
                                 "la probabilidad de ganar ya está corregida.")
    return idea
