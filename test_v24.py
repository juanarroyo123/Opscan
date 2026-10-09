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
    assert s["accounts"]["RSI"]["name"] == "Soportes + RSI" and s["accounts"]["AUTO"]["name"] == "Automática"


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


def test_corregir_precio():
    book = {"trades": [], "seq": 0}
    ok, _ = paper.apply_request(book, {"op": "open", "account": "RSI", "ticker": "DDOG", "direction": "BAJISTA",
                                       "legs": [{"symbol": "DDOG261030P00250000", "kind": "P", "strike": 250,
                                                 "expiration": "2026-10-30", "action": "COMPRAR", "mid": 5.4}],
                                       "contracts": 1, "trade_id": "M0002"}, "2026-10-06")
    assert ok
    req = paper.parse_request("PAPER CORREGIR M0002", '{"id": "M0002", "entry": 5.15, "nonce": "x"}')
    ok, msg = paper.apply_request(book, req, "2026-10-06")
    t = book["trades"][0]
    assert ok and t["entry_cost"] == 515.0 and t["legs"][0]["entry"] == 5.15 and t["pnl"] == 25.0
    assert paper.account(book, "RSI")["cash"] == 10000 - 515
    ok, _ = paper.apply_request(book, {"op": "edit", "id": "M0002"}, "2026-10-06")
    assert not ok                                                       # nada que cambiar


def test_telegram_solo_operaciones():
    from opscan import alerts
    assert alerts.only_trades({"alerts": {}}) and not alerts.only_trades({"alerts": {"mode": "completo"}})
    t = {"ticker": "NFLX", "opened": "2026-10-09", "contracts": 2, "source": "AUTO",
         "legs": [{"kind": "P", "strike": 71.0, "expiration": "2026-11-20", "entry": 2.09}]}
    m = alerts.msg_buy(t)
    assert m.splitlines() == ["OPSCAN | CARTERA AUTOMÁTICA | COMPRA", "", "Fecha: 09/10/2026",
                              "Operación: compra de 2 contratos PUT NFLX strike 71, vencimiento 20/11/2026",
                              "Prima: $2,09 por opción ($418,00 en total)"]


def test_dividendo_dentro_de_la_vida_de_la_call():
    import datetime as dt
    from opscan import dividends, scoring
    dv = dividends.info({"ex_div_date": "2026-07-20", "dividend_rate": 4.0}, 100.0, dt.date(2026, 10, 7),
                        ["2026-01-20", "2026-04-20"])
    assert dv["next_ex"] == "2026-10-19" and dv["estimated"] and dv["amount"] == 1.0
    idea = {"legs": [{"kind": "C", "mid": 2.0, "expiration": "2026-11-20"}],
            "choices": [{"kind": "C", "strike": 105, "mid": 2.0, "breakeven": 107.0, "iv": 0.3, "dte": 44,
                         "expiration": "2026-11-20", "pop": 30.0}]}
    dividends.adjust_idea(idea, dv, 100.0, dt.date(2026, 10, 7), scoring.prob_profit)
    c = idea["choices"][0]
    assert c["pop"] < scoring.prob_profit("C", 100.0, 107.0, 0.3, 44) and c["div_share"] == 0.5
    assert idea["dividend"]["share_of_premium"] == 0.5 and "EN CONTRA" in idea["dividend_note"]
    # el Robot no compra esa call (dividendo = 50% del precio de la opcion)
    paper.configure({"paper": {"capital": 10000, "max_pct_trade": 5}})
    r = {"ticker": "DIV", "score": 70, "direction": "ALCISTA", "price": 100, "checklist": {"entrada": True},
         "idea": {**idea, "legs": [{"symbol": "DIV1", "kind": "C", "strike": 105, "expiration": "2026-11-20",
                                    "action": "COMPRAR", "mid": 2.0}]}}
    assert paper.auto_open({"trades": [], "seq": 0}, [r], "2026-10-07", {"level": "verde"}) == 0


def test_robot_guarda_explicacion():
    book = {"trades": [], "seq": 0}
    r = _rec("XPL", 70)
    r.update(reasons=["4 alerta(s) confirmadas por subida de OI (+20)"], signal="ALTA",
             checklist={"entrada": True, "puntos": 4, "flujo_confirmado": True})
    assert paper.auto_open(book, [r], "2026-10-06", {"level": "verde"}) == 1
    why = book["trades"][0]["why"]
    assert "confirmadas por subida de OI" in why and "Contrato elegido" in why and "Señal alcista" in why
    from opscan import alerts
    m = alerts.msg_buy(book["trades"][0])
    assert "Motivo de la entrada:" in m and "Contrato elegido" in m
    assert all(ord(ch) < 0x2190 or ch in "≥−" for ch in m)              # formal: sin emojis
