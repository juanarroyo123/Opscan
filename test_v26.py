"""act. 21: el giro del flujo solo cierra si es fuerte (score >= 55), se repite 2 sesiones y no es el dia de compra."""
from opscan import paper


def _book(opened):
    book = {"trades": [], "seq": 0}
    leg = [{"symbol": "SMCI1", "kind": "P", "strike": 27, "expiration": "2026-12-11", "action": "COMPRAR",
            "mid": 2.1, "bid": 2.0, "ask": 2.2}]
    t = paper.open_trade(book, "SMCI", "BAJISTA", leg, opened, "AUTO")
    t.update(pnl_pct=-18.6, pnl=-44.0)
    return book, t


def _step(book, session, score, mode="full"):
    recs = {"SMCI": {"direction": "ALCISTA", "score": score}}
    paper.advise(book, recs, session)
    paper.manage_auto(book, session, mode, recs)


def test_giro_debil_solo_vigilar():
    book, t = _book("2026-11-09")
    for d in ("2026-11-10", "2026-11-11", "2026-11-12"):
        _step(book, d, 45.5)
        assert t["advice"]["action"] == "VIGILAR" and t["status"] == "OPEN"


def test_giro_fuerte_el_dia_de_compra_no_cierra():
    book, t = _book("2026-11-10")
    _step(book, "2026-11-10", 60)
    assert t["status"] == "OPEN" and "mismo día" in t["kept_why"]


def test_giro_fuerte_dos_sesiones_seguidas_cierra():
    book, t = _book("2026-11-09")
    _step(book, "2026-11-10", 60)
    _step(book, "2026-11-11", 61, "intraday")    # los escaneos intradia no cuentan
    assert t["status"] == "OPEN"
    _step(book, "2026-11-11", 61)
    assert t["status"] == "CLOSED"


def test_giro_interrumpido_reinicia_la_cuenta():
    book, t = _book("2026-11-09")
    _step(book, "2026-11-10", 60)
    _step(book, "2026-11-11", 40)
    _step(book, "2026-11-12", 60)
    assert t["status"] == "OPEN"


def test_stop_sigue_mandando_el_dia_de_compra():
    book, t = _book("2026-11-10")
    t.update(pnl_pct=-70.0)
    _step(book, "2026-11-10", 60)
    assert t["status"] == "CLOSED" and t["close_reason"].startswith("stop")
