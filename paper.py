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


def _path():
    return os.path.join(state_dir(), "paper_trades.json")


def load():
    return read_json(_path(), {"trades": [], "seq": 0}) or {"trades": [], "seq": 0}


def save(book):
    os.makedirs(state_dir(), exist_ok=True)
    write_json(_path(), book, indent=1)


def _intrinsic(kind, strike, spot):
    if spot is None:
        return 0.0
    return max(0.0, spot - strike) if kind in ("C", "CALL") else max(0.0, strike - spot)


def open_trade(book, ticker, direction, legs, session, source="AUTO", structure="", price=None,
               notes="", issue=None):
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
                      "entry": round(mid, 3), "last": round(mid, 3)})
    cost = round(sum(l["qty"] * l["entry"] for l in clean) * 100, 2)
    if cost <= 0:
        return None
    book["seq"] = int(book.get("seq", 0)) + 1
    t = {"id": f"P{book['seq']:04d}", "source": source, "opened": session, "ticker": ticker.upper(),
         "direction": direction, "structure": structure, "legs": clean, "entry_cost": cost,
         "underlying_entry": price, "status": "OPEN", "value": cost, "pnl": 0.0, "pnl_pct": 0.0,
         "last_update": session, "notes": notes[:300], "issue": issue,
         "expiration": min(l["expiration"] for l in clean)}
    book["trades"].append(t)
    log(f"paper: abierta {t['id']} {ticker} {direction} coste ${cost}")
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
        t["value"] = round(sum(l["qty"] * l["last"] for l in t["legs"]) * 100, 2)
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
    n = 0
    for r in records:
        ck = r.get("checklist") or {}
        idea = r.get("idea") or {}
        if ck.get("entrada") and idea.get("legs") and not has_open(book, r["ticker"], "AUTO"):
            if open_trade(book, r["ticker"], r["direction"], idea["legs"], session, "AUTO",
                          idea.get("structure", ""), r.get("price"),
                          notes="; ".join((r.get("reasons") or [])[:3])):
                n += 1
    return n


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
    }


def export(book):
    tr = sorted(book["trades"], key=lambda t: (t["status"] != "OPEN", t.get("closed") or "", t["opened"]),
                reverse=False)
    return {"generated_at": iso_now(), "summary": summary(book), "trades": tr[-300:],
            "rules": {"target_pct": TARGET_PCT, "stop_pct": STOP_PCT}}


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
        tid = data.get("id") or (re.search(r"P\d{4}", title) or [None])[0]
        return {"op": "close", "id": tid}
    if "ABRIR" in title:
        return {"op": "open", **data}
    return {"op": "unknown"}


def apply_request(book, req, session, issue=None):
    if req.get("op") == "open":
        t = open_trade(book, req.get("ticker", ""), req.get("direction", ""), req.get("legs") or [], session,
                       "MANUAL", req.get("structure", ""), req.get("price"), req.get("notes", ""), issue)
        return (True, f"Abierta {t['id']} ({t['ticker']}) coste ${t['entry_cost']}") if t else (False, "Datos de la operacion no validos")
    if req.get("op") == "close":
        for t in book["trades"]:
            if t["id"] == req.get("id") and t["status"] == "OPEN":
                _close(t, session, "cierre manual")
                return True, f"Cerrada {t['id']} con P&L ${t['pnl']} ({t['pnl_pct']}%)"
        return False, f"No hay operacion abierta con id {req.get('id')}"
    return False, "Titulo no reconocido (usa 'PAPER ABRIR TICKER' o 'PAPER CERRAR P0001')"


def main(argv=None):
    p = argparse.ArgumentParser(prog="opscan.paper")
    p.add_argument("--title", required=True)
    p.add_argument("--body-file", required=True)
    p.add_argument("--issue", default=None)
    p.add_argument("--result-file", default="paper_result.txt")
    a = p.parse_args(argv)
    body = open(a.body_file, encoding="utf-8").read()
    book = load()
    session = today_et().isoformat()
    ok, msg = apply_request(book, parse_request(a.title, body), session, a.issue)
    if ok:
        save(book)
        from .config import data_dir
        write_json(os.path.join(data_dir(), "paper.json"), export(book))
    with open(a.result_file, "w", encoding="utf-8") as f:
        f.write(("OK: " if ok else "ERROR: ") + msg)
    print(msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
