"""Act. 20: reglas nuevas de la cartera automatica (estudio del 10/11/2026)."""
from opscan import paper

NEW = {"capital": 10000, "max_pct_trade": 5, "use_score_bands": True, "bull_score_band": [45, 55],
       "bear_score_band": [45, 65], "intraday_exits": True, "confirm_scans": 2, "hard_stop_pct": -65,
       "earnings_block_days": 7, "earnings_exit_days": 1, "max_entry_spread_pct": 15, "put_size": 0.5,
       "put_hold_winners": True, "require_momentum": True, "auto_min_score": 50, "amber_min_score": 50, "bearish_need_downtrend": False,
       "no_index": True, "max_new_per_day": 3, "max_open_auto": 8, "cooldown_days": 4, "min_dte_auto": 30}


def setup_function(_):
    paper.configure({"paper": dict(NEW)})


def rec(tk, score, direction="ALCISTA", bid=1.9, ask=2.1, earnings=None, exp="2026-12-18", m5=None):
    k = "C" if direction == "ALCISTA" else "P"
    m5 = m5 if m5 is not None else (2.0 if direction == "ALCISTA" else -2.0)
    return {"ticker": tk, "score": score, "direction": direction, "price": 100, "signal": "MEDIA", "tech": {"chg_5d": m5},
            "checklist": {"entrada": False}, "earnings": {"date": earnings} if earnings else None,
            "idea": {"legs": [{"symbol": f"{tk}1", "kind": k, "strike": 100, "expiration": exp,
                               "action": "COMPRAR", "mid": (bid + ask) / 2, "bid": bid, "ask": ask}]}}


def test_bandas_sin_checklist():
    book = {"trades": [], "seq": 0}
    rs = [rec("BULLOK", 50), rec("BULLHI", 58), rec("BEAROK", 60, "BAJISTA"), rec("BEARLO", 40, "BAJISTA")]
    n = paper.auto_open(book, rs, "2026-11-11", {"level": "verde"})
    assert n == 2 and {t["ticker"] for t in book["trades"]} == {"BULLOK", "BEAROK"}
    put = next(t for t in book["trades"] if t["ticker"] == "BEAROK")
    call = next(t for t in book["trades"] if t["ticker"] == "BULLOK")
    assert put["entry_cost"] <= call["entry_cost"] / 2 + 1          # puts a media posicion
    assert put["entry_meta"]["rules"] == "act20" and "banda" in call["why"]


def test_resultados_y_liquidez_al_entrar():
    book = {"trades": [], "seq": 0}
    rs = [rec("EARN", 50, earnings="2026-11-15"), rec("WIDE", 50, bid=1.5, ask=2.5), rec("OK", 50, earnings="2026-12-01")]
    assert paper.auto_open(book, rs, "2026-11-11", {"level": "verde"}) == 1
    assert book["trades"][0]["ticker"] == "OK" and book["trades"][0]["earnings_date"] == "2026-12-01"


def _open(score=50, direction="ALCISTA"):
    book = {"trades": [], "seq": 0}
    paper.auto_open(book, [rec("X", score, direction)], "2026-11-11", {"level": "verde"})
    return book, book["trades"][0]


def _mark(book, t, mid):
    t["legs"][0].update(last=mid, bid=mid, ask=mid)
    t["value"] = round(mid * 100 * t["contracts"], 2)
    t["pnl"] = round(t["value"] - t["entry_cost"], 2)
    t["pnl_pct"] = round(t["pnl"] / t["entry_cost"] * 100, 1)


def test_stop_en_cada_escaneo_con_confirmacion():
    book, t = _open()
    _mark(book, t, 1.0)                                              # -52 %
    paper.manage_auto(book, "2026-11-12", "intraday")
    assert t["status"] == "OPEN"                                     # 1 escaneo: aun no
    paper.manage_auto(book, "2026-11-12", "intraday")
    assert t["status"] == "CLOSED" and t["close_reason"].startswith("stop")
    book, t = _open()
    _mark(book, t, 0.6)                                              # -71 %: salto brusco, cierra ya
    paper.manage_auto(book, "2026-11-12", "intraday")
    assert t["status"] == "CLOSED" and "salto" in t["close_reason"]
    book, t = _open()
    _mark(book, t, 0.6)
    assert paper.manage_auto(book, "2026-11-12", "premarket") == [] and t["status"] == "OPEN"


def test_rebote_reinicia_la_cuenta_del_stop():
    book, t = _open()
    _mark(book, t, 1.0); paper.manage_auto(book, "2026-11-12", "intraday")
    _mark(book, t, 1.3); paper.manage_auto(book, "2026-11-12", "intraday")
    _mark(book, t, 1.0); paper.manage_auto(book, "2026-11-12", "intraday")
    assert t["status"] == "OPEN"


def test_cierra_la_vispera_de_resultados():
    book, t = _open()
    recs = {"X": {"earnings": {"date": "2026-11-20"}}}
    paper.manage_auto(book, "2026-11-18", "full", recs)
    assert t["status"] == "OPEN"
    paper.manage_auto(book, "2026-11-19", "full", recs)
    assert t["status"] == "CLOSED" and "resultados" in t["close_reason"]


def test_put_ganadora_no_se_cierra_por_giro_del_flujo():
    book, t = _open(60, "BAJISTA")
    _mark(book, t, 2.3)                                              # va ganando
    t["advice"] = {"action": "CERRAR", "reasons": ["El flujo de opciones ha girado a ALCISTA (score 40)"]}
    paper.manage_auto(book, "2026-11-12", "full", {})
    assert t["status"] == "OPEN" and "kept_why" in t
    _mark(book, t, 1.8)                                              # va perdiendo
    flip = {t["ticker"]: {"direction": "ALCISTA", "score": 60}}       # act. 21: giro fuerte en 2 sesiones
    t["advice"] = {"action": "CERRAR", "reasons": ["El flujo de opciones ha girado a ALCISTA (score 60)"]}
    paper.manage_auto(book, "2026-11-13", "full", flip)
    assert t["status"] == "OPEN"
    paper.manage_auto(book, "2026-11-14", "full", flip)
    assert t["status"] == "CLOSED"


def test_momento_a_favor():
    book = {"trades": [], "seq": 0}
    rs = [rec("UPC", 50, m5=3.0), rec("DNC", 50, m5=-3.0), rec("DNP", 60, "BAJISTA", m5=-4.0), rec("UPP", 60, "BAJISTA", m5=4.0)]
    assert paper.auto_open(book, rs, "2026-11-11", {"level": "verde"}) == 2
    assert {t["ticker"] for t in book["trades"]} == {"UPC", "DNP"}


def test_put_cara_compra_1_contrato_si_cabe_en_el_maximo():
    book = {"trades": [], "seq": 0}
    r = rec("PUT4", 60, "BAJISTA", bid=3.9, ask=4.1)                   # $410: mas que la mitad ($250), menos que $500
    assert paper.auto_open(book, [r], "2026-11-11", {"level": "verde"}) == 1
    assert book["trades"][0]["contracts"] == 1
    r = rec("PUT6", 60, "BAJISTA", bid=5.9, ask=6.1)                   # $610: no cabe ni en el maximo
    assert paper.auto_open({"trades": [], "seq": 0}, [r], "2026-11-11", {"level": "verde"}) == 0
