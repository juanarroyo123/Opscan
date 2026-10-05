"""Cartera simulada (paper trading) de opciones.

- AUTO: cada ENTRADA del checklist abre una operacion simulada con la idea propuesta
  (1 contrato/spread). Se cierra sola con +100% (objetivo), -50% (stop) o al vencer.
  Es el registro objetivo de "que habria pasado si sigo la estrategia al pie de la letra".
- MANUAL: desde la web, boton "Simular" -> abre un Issue en GitHub -> el workflow
  paper.yml lo anota aqui. Para cerrarla, boton "Cerrar" (otro Issue).

Las posiciones se valoran en cada escaneo con el precio medio (bid+ask)/2 de Cboe.
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


def configure(cfg):
    global CAPITAL, MAX_PCT, MAX_NEW_DAY, MAX_OPEN_AUTO, SKIP_BAD_LIQ
    pc = (cfg or {}).get("paper") or {}
    CAPITAL = float(pc.get("capital", CAPITAL))
    MAX_PCT = float(pc.get("max_pct_trade", MAX_PCT))
    MAX_NEW_DAY = int(pc.get("max_new_per_day", MAX_NEW_DAY))
    MAX_OPEN_AUTO = int(pc.get("max_open_auto", MAX_OPEN_AUTO))
    SKIP_BAD_LIQ = bool(pc.get("skip_bad_liquidity", SKIP_BAD_LIQ))


def _n(t):
    return int(t.get("contracts") or 1)


def account(book, source="MANUAL"):
    """Cuenta de una cartera: MANUAL = la tuya, AUTO = la estrategia sola (cada una con su capital).
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
    for src in ("MANUAL", "AUTO"):
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
               notes="", issue=None, contracts=None, trade_id=None):
    """legs: [{symbol, kind, strike, expiration, action COMPRAR/VENDER, mid}]"""
    if not legs:
        return None
    clean = []
    for l in legs:
        try:
            mid = float(l.get("mid"))
        except (TypeError, ValueError):
            return None
        qty = 1 if str(l.get("action", "COMPRAR")).upper().startswith("COMP") else -1
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
        n = int(acc["max_per_trade"] // unit)
        if n < 1:
            if source == "AUTO":
                book["_last_reject"] = f"1 contrato (${unit}) supera el maximo por operacion (${acc['max_per_trade']})"
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
    t = {"id": tid, "source": source, "opened": session, "ticker": ticker.upper(),
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


def _close(t, session, reason):
    t["status"] = "CLOSED"
    t["closed"] = session
    t["exit_value"] = t["value"]
    t["close_reason"] = reason


def mark(book, prices, spots, session):
    """prices: {ticker: {symbol: {mid,...}}}; spots: {ticker: precio}."""
    for t in book["trades"]:
        if t["status"] != "OPEN":
            continue
        pm = prices.get(t["ticker"], {})
        spot = spots.get(t["ticker"])
        expired = all(l["expiration"] < session for l in t["legs"])
        for l in t["legs"]:
            if l["expiration"] < session:
                l["last"] = round(_intrinsic(l["kind"], l["strike"], spot), 3)
            elif l["symbol"] in pm and pm[l["symbol"]].get("mid", 0) > 0:
                l["last"] = pm[l["symbol"]]["mid"]
        t["value"] = round(sum(l["qty"] * l["last"] for l in t["legs"]) * 100 * _n(t), 2)
        t["pnl"] = round(t["value"] - t["entry_cost"], 2)
        t["pnl_pct"] = round(t["pnl"] / t["entry_cost"] * 100, 1) if t["entry_cost"] else 0.0
        t["underlying_last"] = spot
        t["last_update"] = session
        if expired:
            _close(t, session, "vencimiento")
        elif t["source"] == "AUTO" and t["pnl_pct"] >= TARGET_PCT:
            _close(t, session, f"objetivo +{TARGET_PCT:.0f}%")
        elif t["source"] == "AUTO" and t["pnl_pct"] <= STOP_PCT:
            _close(t, session, f"stop {STOP_PCT:.0f}%")
    return book


def auto_open(book, records, session):
    """Abre las ENTRADAS del dia por orden de score, con limites de cantidad y liquidez."""
    n_today = sum(1 for t in book["trades"] if t["source"] == "AUTO" and t["opened"] == session)
    n_open = sum(1 for t in book["trades"] if t["source"] == "AUTO" and t["status"] == "OPEN")
    n = 0
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
        t = open_trade(book, r["ticker"], r["direction"], idea["legs"], session, "AUTO",
                       idea.get("structure", ""), r.get("price"),
                       notes="; ".join((r.get("reasons") or [])[:3]))
        if t:
            nc = r.get("next_catalyst") or {}
            if nc.get("date") and nc["date"] <= t["expiration"]:
                t["catalyst"] = {"date": nc["date"], "type": nc.get("type")}
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
        rd = r.get("direction")
        if rd in ("ALCISTA", "BAJISTA") and rd != t["direction"] and (r.get("score") or 0) >= media:
            close.append(f"El flujo de opciones ha girado a {rd} (score {r.get('score')})")
        if not close:
            if pnl >= 50:
                watch.append(f"Vas {pnl:+.0f}%: valora vender la mitad y dejar correr el resto")
            if pnl <= -30:
                watch.append(f"Vas {pnl:+.0f}%: cerca del stop (-50%)")
            if cdays is not None and 0 <= cdays <= 2:
                watch.append(f"{cat.get('type')} en {cdays} dia(s): decide si quieres estar dentro durante el evento "
                             "(todo o nada)")
            if r and (r.get("score") or 0) < 15 and not (r.get("checklist") or {}).get("flujo_confirmado"):
                watch.append("La senal se ha enfriado (score bajo y sin flujo confirmado)")
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
        "account": account(book, "MANUAL"), "account_auto": account(book, "AUTO"),
    }


def export(book):
    tr = sorted(book["trades"], key=lambda t: (t["status"] != "OPEN", t.get("closed") or "", t["opened"]),
                reverse=False)
    return {"generated_at": iso_now(), "summary": summary(book), "trades": tr[-300:],
            "rules": {"target_pct": TARGET_PCT, "stop_pct": STOP_PCT, "capital": float(book.get("capital") or CAPITAL),
                      "max_pct_trade": MAX_PCT},
            "curves": {k: v[-250:] for k, v in (book.get("curves") or {}).items()},
            "orders_done": int(book.get("orders_done") or 0),
            "order_results": book.get("order_results", {})}


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
    if "CERRAR" in title:
        tid = data.get("id") or (re.search(r"[PM]\d{4}", title) or [None])[0]
        return {"op": "close", "id": tid, "nonce": data.get("nonce"), "lesson": data.get("lesson")}
    if "ABRIR" in title:
        return {"op": "open", **data}
    return {"op": "unknown"}


def apply_request(book, req, session, issue=None):
    if req.get("op") == "open":
        t = open_trade(book, req.get("ticker", ""), req.get("direction", ""), req.get("legs") or [], session,
                       "MANUAL", req.get("structure", ""), req.get("price"), req.get("notes", ""), issue,
                       contracts=req.get("contracts"), trade_id=req.get("trade_id"))
        if t:
            if req.get("why"):
                t["why"] = str(req["why"])[:300]
            a = account(book, "MANUAL")
            return True, (f"Abierta {t['id']} ({t['ticker']}): {t['contracts']} contrato(s) x ${t['unit_cost']} = "
                          f"${t['entry_cost']}. Saldo disponible: ${a['cash']}")
        return False, book.pop("_last_reject", None) or "Datos de la operacion no validos"
    if req.get("op") == "close":
        for t in book["trades"]:
            if t["id"] == req.get("id") and t["status"] == "OPEN":
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
