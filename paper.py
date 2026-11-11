"""Cartera simulada (paper trading) de opciones. Tres carteras de $10.000 cada una:

- MANUAL  "OpScan": la tuya, con las senales de la web.
- RSI     "Soportes + RSI": la tuya, con tu estrategia propia (no se usa para calibrar la web).
- AUTO    "Robot": cada ENTRADA del checklist abre una operacion sola y la gestiona con reglas fijas
  (decide UNA vez al dia, con el escaneo de cierre: stop -50%, objetivo +100%, a +50% vende la
  mitad y protege el resto, y cierra si su propio consejo dice CERRAR).

Valoracion: precio medio (bid+ask)/2 mientras esta abierta; al CERRAR se usa el bid (lo que te
pagarian de verdad). El Robot compra al ask.
state/paper_trades.json
"""
import argparse
import datetime as dt
import json
import os
import re
import sys

from .config import state_dir
from .util import iso_now, log, parse_date, read_json, today_et, write_json

TARGET_PCT = 100.0
STOP_PCT = -50.0
CAPITAL = 10000.0        # capital inicial de la cartera simulada (config: paper.capital)
MAX_PCT = 5.0            # % maximo del valor de la cartera por operacion (config: paper.max_pct_trade)


MAX_NEW_DAY = 3
MAX_OPEN_AUTO = 8
SKIP_BAD_LIQ = True
RULES_VERSION = 2
HALF_PCT = 50.0          # Robot: a +50% vende la mitad y el resto queda protegido en el precio de entrada
COOLDOWN_DAYS = 4        # Robot: tras cerrar con perdida no vuelve a entrar en ese valor en 4 dias
MIN_DTE_AUTO = 30        # Robot: vencimiento minimo de 30 dias
AMBER_MIN_SCORE = 55     # Robot: con el semaforo en ambar solo entra con score >= 55 (en rojo, nada)
NO_INDEX_PUTS = True     # (antiguo) sustituido por NO_INDEX
NO_INDEX = True          # Automatica: ni calls ni puts de indices, ETF ni VIX (aciertan 36 %, act. 19)
AUTO_MIN_SCORE = 50.0    # Automatica: score minimo 50 (por debajo de 45 las senales aciertan < 50 %, act. 19)
BEAR_NEED_DOWNTREND = True  # Automatica: bajistas solo con la accion por debajo de su media de 50 dias
MAX_DIV_SHARE = 0.30     # Robot: no compra una CALL si el dividendo dentro de su vida es >= 30% de su precio
# --- act. 20 (estudio del 10/11: 29 operaciones cerradas, 2.547 senales) ---
USE_BANDS = True         # entra por bandas de score (el checklist ENTRADA acertaba 33-41 %)
BULL_BAND = (45.0, 55.0) # alcistas: de 55 a 65 acertaban un 32 % a 10 sesiones
BEAR_BAND = (45.0, 65.0) # bajistas: 45-65 acertaban 58-70 %; por debajo de 45 son coberturas
INTRADAY_EXITS = True    # stop y objetivo se revisan en cada escaneo (antes solo al cierre: stops del -71 %)
CONFIRM_SCANS = 2        # dos escaneos seguidos por debajo del stop (o por encima del objetivo)
HARD_STOP_PCT = -65.0    # o uno solo por debajo de -65 % (salto brusco)
EARN_BLOCK_DAYS = 7      # no abre si hay resultados en los proximos 7 dias...
EARN_EXIT_DAYS = 1       # ...y cierra la vispera de los resultados (las puts que los pasaron perdieron -59 %)
MAX_ENTRY_SPREAD = 15.0  # no compra si la diferencia compra/venta supera el 15 %
PUT_SIZE = 0.5           # las puts van a media posicion hasta que demuestren ventaja
PUT_HOLD_WINNERS = True  # una put que va ganando no se cierra solo porque gire el flujo
REQUIRE_MOMENTUM = True  # la accion ya se mueve a favor en 5 dias (con ello: 62 % a 10 sesiones frente a 55 %)
# --- act. 21 (11/11: SMCI e INTC abiertas y cerradas el mismo dia por un giro del flujo) ---
# Tras una senal >= 45, el giro a la direccion contraria acierta solo un 42-44 % a 5/10 sesiones si su score
# es < 55 (n=213 a 5d) y la accion se mueve de media a favor de la posicion original. Con score >= 55: 50-57 %.
FLIP_MIN_SCORE = 55.0    # el giro solo cuenta para cerrar con score >= 55 (antes bastaba 30); por debajo, VIGILAR
FLIP_CONFIRM = 2         # y repetido en 2 escaneos de cierre seguidos (2 sesiones), como el stop
FLIP_NOT_ENTRY_DAY = True  # nunca se cierra por giro el mismo dia de la compra (ahi mandan solo los stops)

ACCOUNTS = {"MANUAL": "OpScan", "RSI": "Soportes + RSI", "AUTO": "Automática"}
USER_ACCOUNTS = ("MANUAL", "RSI")


def configure(cfg):
    global CAPITAL, MAX_PCT, MAX_NEW_DAY, MAX_OPEN_AUTO, SKIP_BAD_LIQ, HALF_PCT, COOLDOWN_DAYS, MIN_DTE_AUTO
    global AMBER_MIN_SCORE, NO_INDEX_PUTS, MAX_DIV_SHARE, NO_INDEX, AUTO_MIN_SCORE, BEAR_NEED_DOWNTREND
    global USE_BANDS, BULL_BAND, BEAR_BAND, INTRADAY_EXITS, CONFIRM_SCANS, HARD_STOP_PCT, EARN_BLOCK_DAYS
    global EARN_EXIT_DAYS, MAX_ENTRY_SPREAD, PUT_SIZE, PUT_HOLD_WINNERS, REQUIRE_MOMENTUM
    global FLIP_MIN_SCORE, FLIP_CONFIRM, FLIP_NOT_ENTRY_DAY
    pc = (cfg or {}).get("paper") or {}
    FLIP_MIN_SCORE = float(pc.get("flip_min_score", FLIP_MIN_SCORE))
    FLIP_CONFIRM = int(pc.get("flip_confirm_scans", FLIP_CONFIRM))
    FLIP_NOT_ENTRY_DAY = bool(pc.get("flip_not_entry_day", FLIP_NOT_ENTRY_DAY))
    USE_BANDS = bool(pc.get("use_score_bands", USE_BANDS))
    BULL_BAND = tuple(float(x) for x in pc.get("bull_score_band", BULL_BAND))
    BEAR_BAND = tuple(float(x) for x in pc.get("bear_score_band", BEAR_BAND))
    INTRADAY_EXITS = bool(pc.get("intraday_exits", INTRADAY_EXITS))
    CONFIRM_SCANS = int(pc.get("confirm_scans", CONFIRM_SCANS))
    HARD_STOP_PCT = float(pc.get("hard_stop_pct", HARD_STOP_PCT))
    EARN_BLOCK_DAYS = int(pc.get("earnings_block_days", EARN_BLOCK_DAYS))
    EARN_EXIT_DAYS = int(pc.get("earnings_exit_days", EARN_EXIT_DAYS))
    MAX_ENTRY_SPREAD = float(pc.get("max_entry_spread_pct", MAX_ENTRY_SPREAD) or 0)
    PUT_SIZE = float(pc.get("put_size", PUT_SIZE))
    PUT_HOLD_WINNERS = bool(pc.get("put_hold_winners", PUT_HOLD_WINNERS))
    REQUIRE_MOMENTUM = bool(pc.get("require_momentum", REQUIRE_MOMENTUM))
    HALF_PCT = float(pc.get("half_at_pct", HALF_PCT))
    COOLDOWN_DAYS = int(pc.get("cooldown_days", COOLDOWN_DAYS))
    MIN_DTE_AUTO = int(pc.get("min_dte_auto", MIN_DTE_AUTO))
    AMBER_MIN_SCORE = float(pc.get("amber_min_score", AMBER_MIN_SCORE))
    NO_INDEX_PUTS = bool(pc.get("no_index_puts", NO_INDEX_PUTS))
    MAX_DIV_SHARE = float(pc.get("max_div_share", MAX_DIV_SHARE))
    NO_INDEX = bool(pc.get("no_index", NO_INDEX))
    AUTO_MIN_SCORE = float(pc.get("auto_min_score", AUTO_MIN_SCORE))
    BEAR_NEED_DOWNTREND = bool(pc.get("bearish_need_downtrend", BEAR_NEED_DOWNTREND))
    CAPITAL = float(pc.get("capital", CAPITAL))
    MAX_PCT = float(pc.get("max_pct_trade", MAX_PCT))
    MAX_NEW_DAY = int(pc.get("max_new_per_day", MAX_NEW_DAY))
    MAX_OPEN_AUTO = int(pc.get("max_open_auto", MAX_OPEN_AUTO))
    SKIP_BAD_LIQ = bool(pc.get("skip_bad_liquidity", SKIP_BAD_LIQ))


def _n(t):
    return int(t.get("contracts") or 1)


def account(book, source="MANUAL"):
    """Cuenta de una cartera: MANUAL = OpScan, RSI = Soportes + RSI, AUTO = Robot (cada una con su capital).
    Saldo = capital - coste de lo abierto + resultado de lo cerrado. Valor total = saldo + valor de lo abierto."""
    cap = float(book.get("capital") or CAPITAL)
    tr = [t for t in book["trades"] if source is None or t["source"] == source]
    invested = sum(t["entry_cost"] for t in tr if t["status"] == "OPEN")
    open_value = sum(t.get("value", t["entry_cost"]) for t in tr if t["status"] == "OPEN")
    realized = sum(t.get("pnl", 0) for t in tr if t["status"] != "OPEN")
    cash = cap - invested + realized
    equity = cash + open_value
    return {"capital": round(cap, 2), "cash": round(cash, 2), "invested": round(invested, 2),
            "open_value": round(open_value, 2), "equity": round(equity, 2),
            "realized": round(realized, 2), "unrealized": round(open_value - invested, 2),
            "return_pct": round((equity / cap - 1) * 100, 2) if cap else None,
            "max_per_trade": round(equity * MAX_PCT / 100, 2), "max_pct": MAX_PCT,
            "n_open": sum(1 for t in tr if t["status"] == "OPEN"),
            "n_closed": sum(1 for t in tr if t["status"] != "OPEN")}


def snapshot(book, session):
    """Guarda el valor de cada cartera por sesion (curva de capital)."""
    curves = book.setdefault("curves", {})
    for src in ACCOUNTS:
        a = account(book, src)
        c = [x for x in curves.get(src, []) if x["date"] != session]
        c.append({"date": session, "equity": a["equity"]})
        curves[src] = sorted(c, key=lambda x: x["date"])[-400:]
    book.pop("equity_curve", None)
    return book


def _path():
    return os.path.join(state_dir(), "paper_trades.json")


def load():
    book = read_json(_path(), {"trades": [], "seq": 0}) or {"trades": [], "seq": 0}
    if int(book.get("rules_version") or 1) < RULES_VERSION:
        # las AUTO abiertas con las reglas antiguas (demasiado laxas) se anulan: no cuentan en la estadistica
        before = len(book["trades"])
        book["trades"] = [t for t in book["trades"] if not (t["source"] == "AUTO" and t["status"] == "OPEN")]
        if before != len(book["trades"]):
            log(f"paper: anuladas {before - len(book['trades'])} operaciones AUTO de las reglas antiguas")
        book["rules_version"] = RULES_VERSION
    return book


def save(book):
    os.makedirs(state_dir(), exist_ok=True)
    write_json(_path(), book, indent=1)


def _intrinsic(kind, strike, spot):
    if spot is None:
        return 0.0
    return max(0.0, spot - strike) if kind in ("C", "CALL") else max(0.0, strike - spot)


def open_trade(book, ticker, direction, legs, session, source="AUTO", structure="", price=None,
               notes="", issue=None, contracts=None, trade_id=None, opened=None, size=1.0):
    """legs: [{symbol, kind, strike, expiration, action COMPRAR/VENDER, mid}]
    El Robot (AUTO) compra al ask si lo hay (como en la realidad); las tuyas al precio que indiques."""
    if not legs:
        return None
    clean = []
    for l in legs:
        try:
            mid = float(l.get("mid"))
        except (TypeError, ValueError):
            return None
        qty = 1 if str(l.get("action", "COMPRAR")).upper().startswith("COMP") else -1
        if source == "AUTO":
            try:
                px = float(l.get("ask") if qty > 0 else l.get("bid"))
                if px > 0:
                    mid = px
            except (TypeError, ValueError):
                pass
        clean.append({"symbol": l["symbol"], "kind": str(l.get("kind", "C"))[0].upper(),
                      "strike": float(l["strike"]), "expiration": l["expiration"], "qty": qty,
                      "entry": round(mid, 3), "last": round(mid, 3),
                      **({"iv": round(float(l["iv"]), 4)} if l.get("iv") not in (None, "") else {})})
    unit = round(sum(l["qty"] * l["entry"] for l in clean) * 100, 2)   # coste de 1 contrato
    if unit <= 0:
        return None
    book.setdefault("capital", CAPITAL)
    acc = account(book, source)
    if contracts:
        n = max(1, int(contracts))
    else:
        budget = acc["max_per_trade"] * size
        n = int(budget // unit)
        if n < 1 and size < 1 and unit <= acc["max_per_trade"]:
            n = 1                         # media posicion: si 1 contrato no cabe en la mitad, 1 contrato sin pasar del maximo
        if n < 1:
            if source == "AUTO":
                book["_last_reject"] = f"1 contrato (${unit}) supera el maximo por operacion (${budget:.0f})"
                return None
            n = 1                         # manual: al menos 1 contrato si hay saldo
    if unit * n > acc["cash"]:
        n = int(acc["cash"] // unit)
        if n < 1:
            book["_last_reject"] = f"saldo insuficiente (${acc['cash']}) para 1 contrato de ${unit}"
            return None
    cost = round(unit * n, 2)
    if trade_id:
        tid = trade_id
    else:
        book["seq"] = int(book.get("seq", 0)) + 1
        tid = f"P{book['seq']:04d}"
    t = {"id": tid, "source": source, "opened": opened or session, "ticker": ticker.upper(),
         "direction": direction, "structure": structure, "legs": clean, "entry_cost": cost,
         "contracts": n, "unit_cost": unit,
         "underlying_entry": price, "status": "OPEN", "value": cost, "pnl": 0.0, "pnl_pct": 0.0,
         "last_update": session, "notes": notes[:300], "issue": issue,
         "expiration": min(l["expiration"] for l in clean)}
    book["trades"].append(t)
    log(f"paper: abierta {t['id']} {ticker} {direction} {n} contrato(s) coste ${cost}")
    return t


def has_open(book, ticker, source=None):
    return any(t["ticker"] == ticker and t["status"] == "OPEN" and (source is None or t["source"] == source)
               for t in book["trades"])


def watch_symbols(book):
    out = {}
    for t in book["trades"]:
        if t["status"] == "OPEN":
            for l in t["legs"]:
                out.setdefault(t["ticker"], set()).add(l["symbol"])
    return out


def _exit_value(t):
    """Lo que te pagarian al cerrar: bid para lo comprado (ask para lo vendido); si no hay, el ultimo."""
    tot = 0.0
    for l in t["legs"]:
        px = l.get("bid") if l["qty"] > 0 else l.get("ask")
        if not px or px <= 0 or l.get("expired"):
            px = l["last"]
        tot += l["qty"] * px
    return round(max(0.0, tot) * 100 * _n(t), 2)


def _close(t, session, reason, at_bid=True):
    if at_bid:
        t["value"] = _exit_value(t)
        t["pnl"] = round(t["value"] - t["entry_cost"], 2)
        t["pnl_pct"] = round(t["pnl"] / t["entry_cost"] * 100, 1) if t["entry_cost"] else 0.0
    t["status"] = "CLOSED"
    t["closed"] = session
    t["exit_value"] = t["value"]
    t["close_reason"] = reason


def _split_half(book, t, session):
    """Vende la mitad de los contratos (operacion hija cerrada); el resto sigue abierto."""
    n = _n(t)
    k = n // 2
    if k < 1:
        return None
    part = json.loads(json.dumps(t))
    part["id"] = f"{t['id']}a"
    part["contracts"] = k
    part["entry_cost"] = round(t["unit_cost"] * k, 2)
    part["value"] = round(t["value"] * k / n, 2)
    _close(part, session, f"mitad vendida a +{HALF_PCT:.0f}%")
    part["advice"] = None
    t["contracts"] = n - k
    t["entry_cost"] = round(t["unit_cost"] * (n - k), 2)
    t["value"] = round(t["value"] * (n - k) / n, 2)
    t["pnl"] = round(t["value"] - t["entry_cost"], 2)
    book["trades"].append(part)
    return part


def mark(book, prices, spots, session):
    """prices: {ticker: {symbol: {mid,bid,ask...}}}; spots: {ticker: precio}.
    Aqui solo se valora (y se cierra lo vencido). Las salidas del Robot las decide manage_auto()
    una vez al dia con el escaneo de cierre, para que un precio puntual raro no dispare el stop."""
    for t in book["trades"]:
        if t["status"] != "OPEN":
            continue
        pm = prices.get(t["ticker"], {})
        spot = spots.get(t["ticker"])
        expired = all(l["expiration"] < session for l in t["legs"])
        for l in t["legs"]:
            if l["expiration"] < session:
                l["last"] = round(_intrinsic(l["kind"], l["strike"], spot), 3)
                l["expired"] = True
            elif l["symbol"] in pm and pm[l["symbol"]].get("mid", 0) > 0:
                q = pm[l["symbol"]]
                l["last"] = q["mid"]
                l["bid"], l["ask"] = q.get("bid"), q.get("ask")
        t["value"] = round(sum(l["qty"] * l["last"] for l in t["legs"]) * 100 * _n(t), 2)
        t["pnl"] = round(t["value"] - t["entry_cost"], 2)
        t["pnl_pct"] = round(t["pnl"] / t["entry_cost"] * 100, 1) if t["entry_cost"] else 0.0
        t["underlying_last"] = spot
        t["last_update"] = session
        if expired:
            _close(t, session, "vencimiento", at_bid=False)
            t["exit_why"] = ("Ha llegado el vencimiento: se liquida por su valor intrínseco "
                             f"(${t['value']:,.0f}" + (", venció sin valor)." if not t["value"] else ")."))
    return book


def _earnings_date(r):
    """Proxima fecha de resultados de un registro del radar (o None)."""
    e = (r or {}).get("earnings") or {}
    if e.get("date"):
        return e["date"]
    for c in (r or {}).get("catalysts") or []:
        if c.get("type") == "Resultados" and (c.get("days") is None or c["days"] >= 0):
            return c.get("date")
    nc = (r or {}).get("next_catalyst") or {}
    return nc.get("date") if nc.get("type") == "Resultados" else None


def _confirm(t, key, cond):
    """Cuenta escaneos seguidos que cumplen `cond`; se reinicia en cuanto deja de cumplirse."""
    c = t.setdefault("_scans", {})
    c[key] = (c.get(key, 0) + 1) if cond else 0
    return c[key]


def manage_auto(book, session, mode="full", recs=None):
    """Reglas de salida de la cartera automatica.
    - En cada escaneo (act. 20): stop con 2 escaneos seguidos <= -50 % o uno <= -65 %; objetivo +100 %
      y venta de la mitad a +50 % con 2 escaneos seguidos; cierre la vispera de resultados.
    - Solo con el escaneo de cierre: el consejo CERRAR (giro del flujo, catalizador pasado, vencimiento)."""
    out = []
    full = mode in ("full", "smoke")
    if not full and (not INTRADAY_EXITS or mode == "premarket"):
        return out
    d0 = parse_date(session)
    for t in list(book["trades"]):
        if t["source"] != "AUTO" or t["status"] != "OPEN":
            continue
        pnl = t.get("pnl_pct") or 0.0
        adv = t.get("advice") or {}
        n_stop = _confirm(t, "stop", pnl <= STOP_PCT)
        n_tgt = _confirm(t, "target", pnl >= TARGET_PCT)
        n_half = _confirm(t, "half", pnl >= HALF_PCT)
        if full:   # el giro del flujo solo se cuenta en el escaneo de cierre (1 por sesion)
            rf = (recs or {}).get(t["ticker"]) or {}
            flip_now = (rf.get("direction") in ("ALCISTA", "BAJISTA") and rf.get("direction") != t["direction"]
                        and (rf.get("score") or 0) >= FLIP_MIN_SCORE)
            n_flip = _confirm(t, "flip", flip_now)
        else:
            n_flip = (t.get("_scans") or {}).get("flip", 0)
        need = 1 if (full and not INTRADAY_EXITS) else CONFIRM_SCANS
        ed = parse_date(_earnings_date((recs or {}).get(t["ticker"])) or t.get("earnings_date"))
        if ed and recs and t["ticker"] in recs:
            t["earnings_date"] = ed.isoformat()
        earn_soon = bool(ed and d0 and 0 <= (ed - d0).days <= EARN_EXIT_DAYS and ed.isoformat() <= t["expiration"])
        if pnl <= HARD_STOP_PCT or n_stop >= need:
            why = "salto brusco" if (pnl <= HARD_STOP_PCT and n_stop < need) else f"{n_stop} escaneo(s) seguidos"
            _close(t, session, f"stop {STOP_PCT:.0f}% ({why})")
            t["exit_why"] = (f"La opción pierde un {pnl:.0f}% ({why}): salta el stop ({STOP_PCT:.0f}%). "
                             "Se corta en el momento, sin esperar al cierre, para no perder más de lo previsto.")
        elif n_tgt >= need:
            _close(t, session, f"objetivo +{TARGET_PCT:.0f}%")
            t["exit_why"] = (f"La opción ha ganado un {pnl:+.0f}%: alcanza el objetivo (+{TARGET_PCT:.0f}%). "
                             "Se asegura la ganancia en vez de esperar a que el tiempo y la volatilidad se la coman.")
        elif t.get("protected") and pnl <= 0:
            _close(t, session, "proteccion: volvio al precio de entrada tras vender la mitad")
            t["exit_why"] = ("Ya se vendió la mitad con ganancia y el resto ha vuelto al precio de compra: "
                             "se cierra para no convertir una operación ganadora en perdedora.")
        elif earn_soon:
            _close(t, session, f"antes de resultados ({ed.isoformat()})")
            t["exit_why"] = (f"La empresa presenta resultados el {_fd(ed.isoformat())}: se cierra antes. Un resultado "
                             "puede mover la acción un 10 % de golpe en contra, y ningún stop llega a tiempo.")
        elif n_half >= need and not t.get("protected"):
            part = _split_half(book, t, session)
            t["protected"] = True
            if part:
                part["exit_why"] = (f"La opción gana un {pnl:+.0f}%: se vende la mitad para asegurar ganancia y el resto "
                                    "sigue abierto, protegido (si vuelve al precio de compra se cierra).")
                out.append((part, "mitad"))
            continue
        elif full and adv.get("action") != "CERRAR" and t.get("kept_why"):
            t.pop("kept_why", None)      # el motivo para mantenerla ya no aplica
            continue
        elif full and adv.get("action") == "CERRAR":
            reasons = adv.get("reasons") or []
            only_flip = reasons and all(x.startswith("El flujo de opciones ha girado") for x in reasons)
            if PUT_HOLD_WINNERS and t["direction"] == "BAJISTA" and pnl > 0 and only_flip:
                t["kept_why"] = ("El flujo ha girado, pero la put va ganando: se mantiene y mandan el objetivo, "
                                 "la venta de la mitad y el stop.")
                continue
            if only_flip and FLIP_NOT_ENTRY_DAY and t.get("opened") == session:
                t["kept_why"] = ("El flujo ha girado el mismo día de la compra: no se cierra por eso (sería pagar la "
                                 "diferencia compra/venta dos veces por ruido). Ese día solo manda el stop.")
                continue
            if only_flip and n_flip < FLIP_CONFIRM:
                t["kept_why"] = (f"El flujo ha girado ({n_flip} de {FLIP_CONFIRM} sesiones seguidas): se espera a que se "
                                 "confirme antes de cerrar. Los giros sueltos aciertan menos de la mitad de las veces.")
                continue
            t.pop("kept_why", None)
            _close(t, session, "consejo CERRAR: " + "; ".join(reasons)[:150])
            t["exit_why"] = "Su propio consejo ha pasado a CERRAR: " + "; ".join(reasons) + "."
        else:
            continue
        out.append((t, t["close_reason"]))
    for t, why in out:
        log(f"paper: automatica {t['id']} {t['ticker']} -> {why} ({t.get('pnl_pct', 0):+.0f}%)")
    return out


def _cooldown(book, ticker, session):
    d0 = parse_date(session)
    for t in book["trades"]:
        if t["source"] == "AUTO" and t["ticker"] == ticker and t["status"] == "CLOSED" and (t.get("pnl") or 0) < 0:
            d = parse_date(t.get("closed"))
            if d and d0 and (d0 - d).days < COOLDOWN_DAYS:
                return True
    return False


def _fd(d):
    p = str(d or "")[:10].split("-")
    return f"{p[2]}/{p[1]}" if len(p) == 3 else str(d or "")


def explain_entry(r, leg, semaforo=None, session=None):
    """Explicacion escrita de por que el Robot compra (se guarda en la operacion y va a Telegram)."""
    ck = r.get("checklist") or {}
    idea = r.get("idea") or {}
    kind = "CALL" if str(leg.get("kind", "C")).upper().startswith("C") else "PUT"
    d0, de = parse_date(session), parse_date(leg.get("expiration"))
    dte = (de - d0).days if d0 and de else leg.get("dte")
    lines = [f"Señal {r.get('direction', '').lower()} con score {r.get('score')} ({r.get('signal')}): "
             + (f"está en la banda que mejor ha acertado ({BULL_BAND[0]:g}–{BULL_BAND[1]:g} en alcistas, "
                f"{BEAR_BAND[0]:g}–{BEAR_BAND[1]:g} en bajistas)." if USE_BANDS else
                f"cumple la estrategia de ENTRADA ({ck.get('puntos', '?')} puntos del checklist).")]
    parts = [n for k, n in (("congreso", "congresistas"), ("futuros", "futuros del sector"),
                            ("directivos", "compras de directivos"), ("flujo_confirmado", "flujo de opciones confirmado por OI"))
             if ck.get(k)]
    if parts:
        lines.append("A favor: " + ", ".join(parts) + ".")
    for x in (r.get("reasons") or [])[:6]:
        lines.append(x.rstrip(".") + ".")
    nc = r.get("next_catalyst") or {}
    if nc.get("date"):
        lines.append(f"Catalizador: {nc.get('type')} el {_fd(nc['date'])} (en {nc.get('days')} día{'' if nc.get('days') == 1 else 's'}), antes del vencimiento.")
    ch = next((c for c in (idea.get("choices") or []) if c.get("symbol") == leg.get("symbol")), {})
    det = [f"{kind} strike {float(leg.get('strike', 0)):g}", f"vence el {_fd(leg.get('expiration'))} ({dte} días)"]
    if leg.get("delta") is not None:
        det.append(f"delta {abs(float(leg['delta'])):.2f}")
    if ch.get("pop") is not None:
        det.append(f"probabilidad de ganar ~{ch['pop']:.0f}% (estimada)")
    if ch.get("breakeven"):
        det.append(f"gana si al vencer la acción está {'por encima' if kind == 'CALL' else 'por debajo'} de ${ch['breakeven']:g}")
    lines.append("Contrato elegido: " + ", ".join(det) + ". Strike moderado: equilibrio entre coste y probabilidad.")
    if idea.get("dividend_note"):
        lines.append(idea["dividend_note"])
    elif r.get("dividend"):
        lines.append("Paga dividendo, pero el próximo ex-dividendo cae después del vencimiento: no afecta.")
    lv = (semaforo or {}).get("level")
    if lv:
        lines.append(f"Mercado: semáforo {lv}" + (" (solo entra con score ≥ 55)." if lv == "ambar" else "."))
    try:
        sp = (float(leg["ask"]) - float(leg["bid"])) / ((float(leg["ask"]) + float(leg["bid"])) / 2) * 100
        lines.append(f"Liquidez: diferencia compra/venta del {sp:.0f} % (máximo {MAX_ENTRY_SPREAD:g} %).")
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        pass
    m5 = (r.get("tech") or {}).get("chg_5d")
    if m5 is not None:
        lines.append(f"Momento: la acción {'sube' if m5 > 0 else 'baja'} un {abs(m5):.1f} % en 5 días, a favor de la operación."
                     if (m5 > 0) == (kind == "CALL") else f"Momento: la acción va {m5:+.1f} % en 5 días.")
    if kind == "PUT" and PUT_SIZE < 1:
        lines.append(f"Tamaño: media posición ({PUT_SIZE*MAX_PCT:g} % de la cartera), porque las puts aún no han demostrado ventaja.")
    lines.append("Salida prevista: +100% objetivo, −50% stop revisado en cada escaneo, a +50% vende la mitad, "
                 "cierre la víspera de resultados, o antes si el consejo pasa a CERRAR.")
    return "\n".join("- " + x for x in lines)


def backfill_why(book, recs, session, semaforo=None):
    """Operaciones del Robot abiertas antes de existir la explicacion: se reconstruye con los datos de hoy."""
    for t in book["trades"]:
        if t["source"] == "AUTO" and t["status"] == "OPEN" and not t.get("why") and t["ticker"] in recs:
            try:
                t["why"] = ("(Reconstruida con los datos de hoy: compró el " + _fd(t["opened"]) + ")\n"
                            + explain_entry(recs[t["ticker"]], t["legs"][0], None, session))[:2500]
            except Exception as e:
                log(f"paper: explicacion {t['ticker']}: {e}")


def auto_open(book, records, session, semaforo=None):
    """Abre las ENTRADAS del dia por orden de score, con limites de cantidad, liquidez, vencimiento,
    semaforo de mercado, enfriamiento tras una perdida y sin puts de indices."""
    from .scoring import INDEX_LIKE
    level = (semaforo or {}).get("level")
    if level == "rojo":
        log("paper: semaforo ROJO, el Robot no abre nada hoy")
        return 0
    d0 = parse_date(session)
    n_today = sum(1 for t in book["trades"] if t["source"] == "AUTO" and t["opened"] == session)
    n_open = sum(1 for t in book["trades"] if t["source"] == "AUTO" and t["status"] == "OPEN")
    n = 0
    def in_band(r):
        lo, hi = BULL_BAND if r.get("direction") == "ALCISTA" else BEAR_BAND
        return lo <= (r.get("score") or 0) < hi
    if USE_BANDS:
        cands = [r for r in records if r.get("direction") in ("ALCISTA", "BAJISTA") and in_band(r)
                 and (r.get("idea") or {}).get("legs")]
    else:
        cands = [r for r in records if (r.get("checklist") or {}).get("entrada") and (r.get("idea") or {}).get("legs")]
    for r in sorted(cands, key=lambda r: -(r.get("score") or 0)):
        idea = r["idea"]
        if n_today >= MAX_NEW_DAY or n_open >= MAX_OPEN_AUTO:
            break
        if has_open(book, r["ticker"]):
            continue
        if SKIP_BAD_LIQ and idea.get("liquidity") == "mala":
            log(f"paper: {r['ticker']} no abierta (liquidez mala)")
            continue
        if not USE_BANDS and (r.get("score") or 0) < AUTO_MIN_SCORE:
            continue
        if level == "ambar" and (r.get("score") or 0) < AMBER_MIN_SCORE:
            continue
        is_index = r["ticker"] in INDEX_LIKE or r["ticker"] in ("VIX", "VIXY", "UVXY") or \
            bool({"etf", "indice"} & set(r.get("groups") or []))
        if (NO_INDEX and is_index) or (NO_INDEX_PUTS and r["ticker"] in INDEX_LIKE and r.get("direction") == "BAJISTA"):
            continue
        if BEAR_NEED_DOWNTREND and r.get("direction") == "BAJISTA" and (r.get("tech") or {}).get("above50") is not False:
            log(f"paper: {r['ticker']} bajista no abierta (la accion no esta por debajo de su media de 50 dias)")
            continue
        exps = [parse_date(l.get("expiration")) for l in idea["legs"]]
        if d0 and exps and all(exps) and (min(exps) - d0).days < MIN_DTE_AUTO:
            log(f"paper: {r['ticker']} no abierta (vence en menos de {MIN_DTE_AUTO} dias)")
            continue
        if _cooldown(book, r["ticker"], session):
            log(f"paper: {r['ticker']} no abierta (cerro con perdida hace menos de {COOLDOWN_DAYS} dias)")
            continue
        if REQUIRE_MOMENTUM:
            m5 = (r.get("tech") or {}).get("chg_5d")
            sign = 1 if r.get("direction") == "ALCISTA" else -1
            if m5 is None or m5 * sign <= 0:
                log(f"paper: {r['ticker']} no abierta (la accion no se mueve a favor en 5 dias: {m5})")
                continue
        ed = parse_date(_earnings_date(r))
        if d0 and ed and 0 <= (ed - d0).days <= EARN_BLOCK_DAYS:
            log(f"paper: {r['ticker']} no abierta (resultados el {ed.isoformat()}, en {(ed - d0).days} dias)")
            continue
        leg0 = idea["legs"][0]
        try:
            b_, a_ = float(leg0.get("bid") or 0), float(leg0.get("ask") or 0)
            spread = (a_ - b_) / ((a_ + b_) / 2) * 100 if b_ > 0 and a_ > 0 else None
        except (TypeError, ValueError):
            spread = None
        if MAX_ENTRY_SPREAD and (spread is None or spread > MAX_ENTRY_SPREAD):
            log(f"paper: {r['ticker']} no abierta (diferencia compra/venta {spread if spread is None else round(spread)} %)")
            continue
        dv = idea.get("dividend") or {}
        if str(dv.get("kind", "")).upper().startswith("C") and (dv.get("share_of_premium") or 0) >= MAX_DIV_SHARE:
            log(f"paper: {r['ticker']} no abierta (el dividendo antes del vencimiento es el "
                f"{dv['share_of_premium']*100:.0f}% del precio de la call)")
            continue
        t = open_trade(book, r["ticker"], r["direction"], idea["legs"], session, "AUTO",
                       idea.get("structure", ""), r.get("price"),
                       notes="; ".join((r.get("reasons") or [])[:3]),
                       size=PUT_SIZE if r.get("direction") == "BAJISTA" else 1.0)
        if t:
            t["entry_meta"] = {"score": r.get("score"), "signal": r.get("signal"),
                               "spread_pct": round(spread, 1) if spread is not None else None,
                               "flujo_confirmado": bool((r.get("checklist") or {}).get("flujo_confirmado")),
                               "entrada_checklist": bool((r.get("checklist") or {}).get("entrada")),
                               "earnings_date": ed.isoformat() if ed else None, "rules": "act20"}
            if ed:
                t["earnings_date"] = ed.isoformat()
            nc = r.get("next_catalyst") or {}
            if nc.get("date") and nc["date"] <= t["expiration"]:
                t["catalyst"] = {"date": nc["date"], "type": nc.get("type")}
            try:
                t["why"] = explain_entry(r, idea["legs"][0], semaforo, session)[:2500]
            except Exception as e:
                log(f"paper: explicacion {r['ticker']}: {e}")
            n += 1
            n_today += 1
            n_open += 1
        else:
            why = book.pop("_last_reject", None)
            if why:
                log(f"paper: {r['ticker']} no abierta ({why})")
    return n


def advise(book, recs, session, media=30):
    """Recomendacion de gestion para cada posicion ABIERTA: MANTENER / VIGILAR / CERRAR.
    Devuelve la lista de operaciones cuya recomendacion ha pasado a CERRAR en esta ejecucion."""
    d0 = parse_date(session)
    changed = []
    for t in book["trades"]:
        if t["status"] != "OPEN":
            continue
        r = recs.get(t["ticker"]) or {}
        nc = r.get("next_catalyst") or {}
        if not t.get("catalyst") and nc.get("date") and nc.get("date") <= t["expiration"]:
            t["catalyst"] = {"date": nc["date"], "type": nc.get("type")}
        cat = t.get("catalyst") or {}
        pnl = t.get("pnl_pct") or 0.0
        exp = parse_date(t["expiration"])
        dte = (exp - d0).days if exp and d0 else None
        cdate = parse_date(cat.get("date")) if cat else None
        cdays = (cdate - d0).days if cdate and d0 else None
        close, watch, keep = [], [], []
        if pnl >= TARGET_PCT:
            close.append(f"Objetivo cumplido ({pnl:+.0f}%): asegura la ganancia")
        if pnl <= STOP_PCT:
            close.append(f"Stop alcanzado ({pnl:+.0f}%): corta la perdida, no esperes a que vuelva")
        if dte is not None and dte <= 7:
            close.append(f"Quedan {dte} dias para el vencimiento: el paso del tiempo se come la prima muy rapido")
        if cdays is not None and cdays < 0:
            close.append(f"El catalizador ({cat.get('type')} {cat.get('date')}) ya paso: la volatilidad cae y la "
                         "opcion pierde valor aunque no se mueva la accion")
        own = t["source"] == "RSI"          # tu estrategia propia: no se juzga con el flujo de opciones
        rd = r.get("direction")
        weak_flip = None
        if not own and rd in ("ALCISTA", "BAJISTA") and rd != t["direction"] and (r.get("score") or 0) >= media:
            if (r.get("score") or 0) >= FLIP_MIN_SCORE:
                close.append(f"El flujo de opciones ha girado a {rd} (score {r.get('score')})")
            else:
                weak_flip = (f"El flujo ha girado a {rd} pero con fuerza moderada (score {r.get('score')}, < "
                             f"{FLIP_MIN_SCORE:g}): estos giros aciertan menos de la mitad de las veces; no basta para cerrar")
        if not close:
            if weak_flip:
                watch.append(weak_flip)
            if pnl >= 50:
                watch.append(f"Vas {pnl:+.0f}%: valora vender la mitad y dejar correr el resto")
            if pnl <= -30:
                watch.append(f"Vas {pnl:+.0f}%: cerca del stop (-50%)")
            if cdays is not None and 0 <= cdays <= 2:
                watch.append(f"{cat.get('type')} en {cdays} dia(s): decide si quieres estar dentro durante el evento "
                             "(todo o nada)")
            if own:
                pass
            elif r and (r.get("score") or 0) < 15 and not (r.get("checklist") or {}).get("flujo_confirmado"):
                watch.append("La senal se ha enfriado (score bajo y sin flujo confirmado)")
            elif r and not (r.get("checklist") or {}).get("flujo_confirmado"):
                watch.append("Ya no hay flujo de opciones confirmado a favor: la razon principal de la idea se ha debilitado")
            if not watch:
                if cdays is not None and cdays > 2:
                    keep.append(f"Tesis intacta; {cat.get('type')} en {cdays} dias")
                else:
                    keep.append("Tesis intacta")
                if dte is not None:
                    keep.append(f"vence en {dte} dias")
        action = "CERRAR" if close else ("VIGILAR" if watch else "MANTENER")
        prev = (t.get("advice") or {}).get("action")
        t["advice"] = {"action": action, "reasons": close or watch or keep, "date": session}
        if action == "CERRAR" and prev != "CERRAR":
            changed.append(t)
    return changed


def summary(book):
    tr = book["trades"]
    closed = [t for t in tr if t["status"] == "CLOSED"]
    opened = [t for t in tr if t["status"] == "OPEN"]

    def agg(ts):
        if not ts:
            return {"n": 0, "pnl": 0.0, "win_rate": None, "cost": 0.0}
        pnl = sum(t["pnl"] for t in ts)
        cost = sum(t["entry_cost"] for t in ts)
        wins = sum(1 for t in ts if t["pnl"] > 0)
        return {"n": len(ts), "pnl": round(pnl, 2), "cost": round(cost, 2),
                "ret_pct": round(pnl / cost * 100, 1) if cost else None,
                "win_rate": round(wins / len(ts) * 100, 1)}
    return {
        "open": agg(opened), "closed": agg(closed),
        "auto_closed": agg([t for t in closed if t["source"] == "AUTO"]),
        "manual_closed": agg([t for t in closed if t["source"] == "MANUAL"]),
        "rsi_closed": agg([t for t in closed if t["source"] == "RSI"]),
        "account": account(book, "MANUAL"), "account_auto": account(book, "AUTO"),
        "account_rsi": account(book, "RSI"),
        "accounts": {k: {"name": v, **account(book, k), "closed": agg([t for t in closed if t["source"] == k])}
                     for k, v in ACCOUNTS.items()},
    }


def export(book):
    tr = sorted(book["trades"], key=lambda t: (t["status"] != "OPEN", t.get("closed") or "", t["opened"]),
                reverse=False)
    return {"generated_at": iso_now(), "summary": summary(book), "trades": tr[-300:],
            "rules": {"target_pct": TARGET_PCT, "stop_pct": STOP_PCT, "capital": float(book.get("capital") or CAPITAL),
                      "max_pct_trade": MAX_PCT, "half_at_pct": HALF_PCT, "cooldown_days": COOLDOWN_DAYS,
                      "min_dte_auto": MIN_DTE_AUTO, "amber_min_score": AMBER_MIN_SCORE,
                      "no_index_puts": NO_INDEX_PUTS, "no_index": NO_INDEX, "auto_min_score": AUTO_MIN_SCORE,
                      "bearish_need_downtrend": BEAR_NEED_DOWNTREND, "accounts": ACCOUNTS,
                      "use_score_bands": USE_BANDS, "bull_score_band": list(BULL_BAND), "bear_score_band": list(BEAR_BAND),
                      "intraday_exits": INTRADAY_EXITS, "confirm_scans": CONFIRM_SCANS, "hard_stop_pct": HARD_STOP_PCT,
                      "earnings_block_days": EARN_BLOCK_DAYS, "earnings_exit_days": EARN_EXIT_DAYS,
                      "max_entry_spread_pct": MAX_ENTRY_SPREAD, "put_size": PUT_SIZE, "put_hold_winners": PUT_HOLD_WINNERS,
                      "require_momentum": REQUIRE_MOMENTUM, "flip_min_score": FLIP_MIN_SCORE,
                      "flip_confirm_scans": FLIP_CONFIRM, "flip_not_entry_day": FLIP_NOT_ENTRY_DAY},
            "curves": {k: v[-250:] for k, v in (book.get("curves") or {}).items()},
            "orders_done": int(book.get("orders_done") or 0),
            "order_results": book.get("order_results", {})}


def seed(book, items, session):
    """Operaciones ya hechas fuera de la web (config paper.seed). Cada una se mete UNA sola vez."""
    done = set(book.get("seeded") or [])
    n = 0
    for it in items or []:
        key = it.get("key") or f"{it.get('account')}:{it.get('symbol')}:{it.get('opened')}"
        if key in done:
            continue
        sym = str(it["symbol"]).upper()
        m = re.match(r"^([A-Z.]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$", sym)
        if not m:
            log(f"paper: seed {sym} no valido")
            continue
        exp = f"20{m.group(2)}-{m.group(3)}-{m.group(4)}"
        kind = m.group(5)
        leg = {"symbol": sym, "kind": kind, "strike": int(m.group(6)) / 1000, "expiration": exp,
               "action": "COMPRAR", "mid": it["price"]}
        src = str(it.get("account") or "RSI").upper()
        book["rseq"] = int(book.get("rseq", 0)) + 1
        t = open_trade(book, m.group(1), "ALCISTA" if kind == "C" else "BAJISTA", [leg], session, src,
                       "Call comprada" if kind == "C" else "Put comprada", it.get("underlying"),
                       it.get("notes", ""), contracts=it.get("contracts", 1),
                       trade_id=f"R{book['rseq']:04d}", opened=it.get("opened"))
        if t:
            if it.get("why"):
                t["why"] = str(it["why"])[:300]
            done.add(key)
            n += 1
    book["seeded"] = sorted(done)
    return n


# ------------------------------------------------------------------ peticiones desde GitHub Issues
def parse_request(title, body):
    """Titulo 'PAPER ABRIR XYZ' / 'PAPER CERRAR P0007'. Cuerpo: JSON (puede venir entre ```)."""
    title = (title or "").strip().upper()
    m = re.search(r"\{.*\}", body or "", re.S)
    data = {}
    if m:
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            data = {}
    if "CORREGIR" in title:
        tid = data.get("id") or (re.search(r"[PMR]\d{4}", title) or [None])[0]
        return {"op": "edit", **data, "id": tid}
    if "CERRAR" in title:
        tid = data.get("id") or (re.search(r"[PMR]\d{4}", title) or [None])[0]
        return {"op": "close", "id": tid, "nonce": data.get("nonce"), "lesson": data.get("lesson")}
    if "ABRIR" in title:
        return {"op": "open", **data}
    return {"op": "unknown"}


def edit_trade(book, req, session):
    """Corrige una operacion TUYA abierta: precio pagado por opcion, contratos, fecha de compra, motivo."""
    t = next((x for x in book["trades"] if x["id"] == req.get("id") and x["status"] == "OPEN"
              and x["source"] in USER_ACCOUNTS), None)
    if not t:
        return False, f"No hay operacion tuya abierta con id {req.get('id')}"
    changes = []
    if req.get("entry") not in (None, ""):
        if len(t["legs"]) != 1:
            return False, "Solo se puede corregir el precio de operaciones de una sola opcion"
        try:
            px = round(float(req["entry"]), 3)
        except (TypeError, ValueError):
            return False, "Precio no valido"
        if px <= 0:
            return False, "El precio tiene que ser mayor que 0"
        old = t["legs"][0]["entry"]
        t["legs"][0]["entry"] = px
        t["unit_cost"] = round(px * 100 * t["legs"][0]["qty"], 2)
        changes.append(f"precio ${old} -> ${px}")
    if req.get("contracts") not in (None, ""):
        n = int(req["contracts"])
        if n < 1:
            return False, "Minimo 1 contrato"
        if n != _n(t):
            changes.append(f"contratos {_n(t)} -> {n}")
            t["value"] = round(t["value"] / _n(t) * n, 2)
            t["contracts"] = n
    od = parse_date(req.get("opened"))
    if od and od.isoformat() <= session and od.isoformat() != t["opened"]:
        changes.append(f"fecha {t['opened']} -> {od.isoformat()}")
        t["opened"] = od.isoformat()
    if req.get("why") is not None and str(req["why"]).strip() and str(req["why"]) != t.get("why"):
        t["why"] = str(req["why"])[:300]
        changes.append("motivo")
    if not changes:
        return False, "No hay nada que cambiar"
    t["entry_cost"] = round(t["unit_cost"] * _n(t), 2)
    t["pnl"] = round(t["value"] - t["entry_cost"], 2)
    t["pnl_pct"] = round(t["pnl"] / t["entry_cost"] * 100, 1) if t["entry_cost"] else 0.0
    t.setdefault("edits", []).append({"date": session, "what": ", ".join(changes)})
    return True, f"Corregida {t['id']} ({t['ticker']}): " + ", ".join(changes) + f". Coste ahora ${t['entry_cost']}"


def apply_request(book, req, session, issue=None):
    if req.get("op") == "open":
        src = str(req.get("account") or "MANUAL").upper()
        if src not in USER_ACCOUNTS:
            return False, f"Cartera no valida: {src}"
        opened = None
        od = parse_date(req.get("opened"))
        if od and od.isoformat() <= session:
            opened = od.isoformat()
        t = open_trade(book, req.get("ticker", ""), req.get("direction", ""), req.get("legs") or [], session,
                       src, req.get("structure", ""), req.get("price"), req.get("notes", ""), issue,
                       contracts=req.get("contracts"), trade_id=req.get("trade_id"), opened=opened)
        if t:
            if req.get("why"):
                t["why"] = str(req["why"])[:300]
            a = account(book, src)
            return True, (f"Abierta {t['id']} ({t['ticker']}) en {ACCOUNTS[src]}: {t['contracts']} contrato(s) x ${t['unit_cost']} = "
                          f"${t['entry_cost']}. Saldo disponible: ${a['cash']}")
        return False, book.pop("_last_reject", None) or "Datos de la operacion no validos"
    if req.get("op") == "edit":
        return edit_trade(book, req, session)
    if req.get("op") == "close":
        for t in book["trades"]:
            if t["id"] == req.get("id") and t["status"] == "OPEN" and t["source"] in USER_ACCOUNTS:
                _close(t, session, "cierre manual")
                if req.get("lesson"):
                    t["lesson"] = str(req["lesson"])[:300]
                return True, f"Cerrada {t['id']} con P&L ${t['pnl']} ({t['pnl_pct']}%)"
        return False, f"No hay operacion abierta con id {req.get('id')}"
    return False, "Titulo no reconocido (usa 'PAPER ABRIR TICKER' o 'PAPER CERRAR M0001')"


# ------------------------------------------------------------------ cola de ordenes (rama `orders`)
# La web (o un Issue) solo AGREGA ordenes a orders/orders.json. El escaner las aplica al empezar,
# asi nunca se pisan datos aunque haya un escaneo en marcha. preview.json = cartera con las
# ordenes pendientes ya aplicadas, para verlas al momento en la web.
def load_orders(orders_dir):
    if not orders_dir:
        return []
    return (read_json(os.path.join(orders_dir, "orders.json"), {"orders": []}) or {}).get("orders", [])


def add_order(orders_dir, title, body, source="web"):
    os.makedirs(orders_dir, exist_ok=True)
    orders = load_orders(orders_dir)
    n = max([o["n"] for o in orders] or [0]) + 1
    req = parse_request(title, body)
    o = {"n": n, "ts": iso_now(), "title": (title or "").strip(), "req": req, "source": source,
         "nonce": str(req.get("nonce") or "")}
    orders.append(o)
    write_json(os.path.join(orders_dir, "orders.json"), {"orders": orders[-500:]}, indent=1)
    return o


def apply_orders(book, orders, session):
    """Aplica en orden las ordenes con n > orders_done. Ids estables: M + numero de orden."""
    done = int(book.get("orders_done") or 0)
    res = book.setdefault("order_results", {})
    applied = []
    for o in sorted(orders, key=lambda x: x["n"]):
        if o["n"] <= done:
            continue
        req = dict(o.get("req") or {})
        if req.get("op") == "open":
            req["trade_id"] = f"M{o['n']:04d}"
        ok, msg = apply_request(book, req, session)
        key = o.get("nonce") or str(o["n"])
        res[key] = {"ok": ok, "msg": msg, "n": o["n"], "ts": o.get("ts")}
        book["orders_done"] = o["n"]
        applied.append((o, ok, msg))
        log(f"paper: orden {o['n']} {o.get('title')} -> {'OK' if ok else 'ERROR'}: {msg}")
    if len(res) > 200:
        for k in sorted(res, key=lambda k: res[k].get("n", 0))[:-200]:
            res.pop(k, None)
    return applied


def main(argv=None):
    """Lo usa el workflow paper.yml: agrega la orden a la cola y genera la vista previa."""
    p = argparse.ArgumentParser(prog="opscan.paper")
    p.add_argument("--title", required=True)
    p.add_argument("--body-file", required=True)
    p.add_argument("--issue", default=None)
    p.add_argument("--orders-dir", default="orders")
    p.add_argument("--result-file", default="paper_result.txt")
    a = p.parse_args(argv)
    body = open(a.body_file, encoding="utf-8").read()
    from .config import load_config
    try:
        configure(load_config())
    except Exception:
        pass
    o = add_order(a.orders_dir, a.title, body, "issue" if a.issue else "web")
    book = load()                                       # cartera de la rama data (solo lectura)
    applied = apply_orders(book, load_orders(a.orders_dir), today_et().isoformat())
    out = export(book)
    out["preview"] = True
    write_json(os.path.join(a.orders_dir, "preview.json"), out)
    mine = [x for x in applied if x[0]["n"] == o["n"]]
    ok, msg = (mine[0][1], mine[0][2]) if mine else (False, "orden no aplicada")
    with open(a.result_file, "w", encoding="utf-8") as f:
        f.write(("OK: " if ok else "ERROR: ") + msg)
    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
