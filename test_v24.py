"""Act. 17: tres carteras (OpScan / Soportes + RSI / Robot) y reglas nuevas del Robot."""
from opscan import paper


def _leg(sym="X261120C00020000", k=20, mid=1.0, exp="2026-11-20", bid=None, ask=None):
    return [{"symbol": sym, "kind": "C", "strike": k, "expiration": exp, "action": "COMPRAR", "mid": mid,
             "bid": bid, "ask": ask}]


def setup_function(_):
    paper.configure({"paper": {"capital": 10000, "max_pct_trade": 5}})


def test_seed_rsi_una_vez():
    book = {"trades": [], "seq": 0}
    items = [{"account": "RSI", "symbol": "CHWY261030C00020000", "price": 0.28, "contracts": 1, "opened": "2026-10-05"},
             {"account": "RSI", "symbol": "HD261030C00300000", "price": 2.32, "contracts": 1, "opened": "2026-10-05"}]
    assert paper.seed(book, items, "2026-10-06") == 2
    assert paper.seed(book, items, "2026-10-07") == 0                  # no se duplica
    t = book["trades"][0]
    assert t["source"] == "RSI" and t["opened"] == "2026-10-05" and t["entry_cost"] == 28.0
    assert t["legs"][0]["strike"] == 20.0 and t["legs"][0]["expiration"] == "2026-10-30" and t["id"] == "R0001"
    a = paper.account(book, "RSI")
    assert a["cash"] == 10000 - 28 - 232 and paper.account(book, "MANUAL")["cash"] == 10000
    s = paper.summary(book)
    assert s["accounts"]["RSI"]["name"] == "Soportes + RSI" and s["accounts"]["AUTO"]["name"] == "Robot"


def test_orden_web_cartera_rsi_y_fecha():
    book = {"trades": [], "seq": 0}
    ok, msg = paper.apply_request(book, {"op": "open", "account": "RSI", "ticker": "EFX", "direction": "ALCISTA",
                                         "legs": _leg("EFX261016C00150000", 150, 1.53, "2026-10-16"),
                                         "contracts": 1, "opened": "2026-10-05"}, "2026-10-06")
    assert ok and "Soportes + RSI" in msg and book["trades"][0]["opened"] == "2026-10-05"
    ok, msg = paper.apply_request(book, {"op": "open", "account": "AUTO", "ticker": "Z", "legs": _leg()}, "2026-10-06")
    assert not ok                                                       # nadie mete ordenes en el Robot


def test_robot_compra_al_ask_y_vende_al_bid():
    book = {"trades": [], "seq": 0}
    t = paper.open_trade(book, "X", "ALCISTA", _leg(mid=2.0, bid=1.9, ask=2.1), "2026-10-01", "AUTO")
    assert t["legs"][0]["entry"] == 2.1
    paper.mark(book, {"X": {"X261120C00020000": {"mid": 1.0, "bid": 0.9, "ask": 1.1}}}, {"X": 18}, "2026-10-02")
    assert t["status"] == "OPEN"                                        # mark ya no cierra por stop
    paper.manage_auto(book, "2026-10-02")
    assert t["status"] == "CLOSED" and t["exit_value"] == 90.0 * t["contracts"]


def test_robot_mitad_a_50_y_proteccion():
    book = {"trades": [], "seq": 0}
    t = paper.open_trade(book, "X", "ALCISTA", _leg(mid=1.0), "2026-10-01", "AUTO")
    n = t["contracts"]
    paper.mark(book, {"X": {"X261120C00020000": {"mid": 1.6, "bid": 1.55, "ask": 1.65}}}, {"X": 21}, "2026-10-02")
    paper.manage_auto(book, "2026-10-02")
    half = [x for x in book["trades"] if x["id"] == t["id"] + "a"][0]
    assert half["status"] == "CLOSED" and half["contracts"] == n // 2 and t["contracts"] == n - n // 2
    assert t["protected"] and t["status"] == "OPEN"
    paper.mark(book, {"X": {"X261120C00020000": {"mid": 0.95, "bid": 0.9, "ask": 1.0}}}, {"X": 20}, "2026-10-03")
    paper.manage_auto(book, "2026-10-03")
    assert t["status"] == "CLOSED" and "proteccion" in t["close_reason"]


def test_robot_obedece_cerrar():
    book = {"trades": [], "seq": 0}
    t = paper.open_trade(book, "X", "ALCISTA", _leg(mid=1.0), "2026-10-01", "AUTO")
    paper.advise(book, {"X": {"direction": "BAJISTA", "score": 40}}, "2026-10-02")
    assert t["advice"]["action"] == "CERRAR"
    paper.manage_auto(book, "2026-10-02")
    assert t["status"] == "CLOSED" and t["close_reason"].startswith("consejo")


def _rec(tk, score=60, direction="ALCISTA", exp="2026-11-20"):
    return {"ticker": tk, "score": score, "direction": direction, "price": 20,
            "checklist": {"entrada": True},
            "idea": {"legs": [{"symbol": f"{tk}1", "kind": "C", "strike": 20, "expiration": exp,
                               "action": "COMPRAR", "mid": 1.0}]}}


def test_robot_filtros_entrada():
    book = {"trades": [], "seq": 0}
    recs = [_rec("A", 50), _rec("IWM", 70, "BAJISTA"), _rec("B", 70, exp="2026-10-20"), _rec("C", 60)]
    assert paper.auto_open(book, recs, "2026-10-06", {"level": "rojo"}) == 0
    n = paper.auto_open(book, recs, "2026-10-06", {"level": "ambar"})
    assert n == 1 and book["trades"][0]["ticker"] == "C"                # A score<55, IWM put, B vence pronto
    # enfriamiento: C cerro con perdida -> no se reabre en 4 dias
    book["trades"][0].update(status="CLOSED", closed="2026-10-06", pnl=-50)
    assert paper.auto_open(book, [_rec("C", 60)], "2026-10-07", {"level": "verde"}) == 0
    assert paper.auto_open(book, [_rec("C", 60)], "2026-10-12", {"level": "verde"}) == 1


def test_rsi_sin_consejos_de_flujo():
    book = {"trades": [], "seq": 0}
    paper.seed(book, [{"account": "RSI", "symbol": "HD261030C00300000", "price": 2.32}], "2026-10-06")
    paper.advise(book, {"HD": {"direction": "BAJISTA", "score": 70, "checklist": {}}}, "2026-10-06")
    assert book["trades"][0]["advice"]["action"] == "MANTENER"
