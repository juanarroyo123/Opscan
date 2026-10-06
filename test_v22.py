"""Tests de las funciones nuevas (v2.2)."""
import datetime as dt

import pandas as pd
import pytest

from opscan import enrich, options, paper, pipeline, scoring, shorts, tracking, universe
from opscan.config import load_config
from tests.fakes import cboe_payload

SESSION = dt.date(2026, 9, 30)


@pytest.fixture
def cfg():
    return load_config()


def test_earnings_reactions_bmo_amc():
    idx = pd.date_range("2026-01-01", periods=60, freq="B")
    close = pd.Series([100.0] * 60, index=idx)
    close.iloc[20] = 110.0          # dia de resultados antes de apertura: +10%
    close.iloc[41] = 95.0           # dia siguiente a resultados tras cierre: -5%
    close.iloc[42:] = 95.0
    close.iloc[21:41] = 100.0
    d_bmo = pd.Timestamp(idx[20].date()).tz_localize("America/New_York") + pd.Timedelta(hours=7)
    d_amc = pd.Timestamp(idx[40].date()).tz_localize("America/New_York") + pd.Timedelta(hours=16)
    ed = pd.DataFrame(index=[d_amc, d_bmo])
    moves = enrich.earnings_reactions(ed, close)
    by = {m["when"]: m["move_pct"] for m in moves}
    assert by["antes de apertura"] == 10.0 and by["tras cierre"] == -5.0


def test_earnings_vs_expected():
    hist = {"avg_abs": 4.0, "median_abs": 3.5}
    em = [{"expiration": "2026-10-16", "dte": 15, "em_pct": 6.0}, {"expiration": "2026-10-23", "dte": 22, "em_pct": 7.0}]
    r = enrich.earnings_vs_expected(hist, em, "2026-10-20")
    assert r["expiration"] == "2026-10-23" and r["ratio"] == 1.75 and r["label"] == "caras"


def test_finra_parse_and_squeeze():
    txt = "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n20260930|DYN|600|0|1000|B,Q,N\n"
    d = shorts.parse_finra(txt)
    assert d["DYN"] == (600.0, 1000.0)
    sq = shorts.squeeze_risk(0.22, 0.6, "ALCISTA", 3.0, 2.0, 3.0)
    assert sq["level"] == "ALTO"
    assert shorts.squeeze_risk(0.02, 0.3, "ALCISTA", 3.0, 2.0, 3.0) is None


def test_parse_ishares():
    txt = ('iShares Russell 1000 ETF\nFund Holdings as of,"Oct 01, 2026"\n\n'
           'Ticker,Name,Sector,Asset Class,Market Value\nAAPL,APPLE INC,Information Technology,Equity,1\n'
           'BRKB,BERKSHIRE HATHAWAY INC CLASS B,Financials,Equity,1\nUSD,USD CASH,Cash and/or Derivatives,Cash,1\n')
    d = universe.parse_ishares_csv(txt, known={"BRK.B"})
    assert set(d) == {"AAPL", "BRK.B"} and d["AAPL"]["sector"] == "Information Technology"


def test_tracking_evaluate():
    sig = tracking.record_signals(tracking.load_signals(), [
        {"ticker": "XYZ", "direction": "ALCISTA", "price": 100.0, "signal": "MEDIA", "score": 40,
         "checklist": {"entrada": True, "flujo_confirmado": True}, "next_catalyst": {"type": "PDUFA"}}],
        "2026-09-01")
    # mismo valor+direccion 2 dias despues: no se duplica
    sig = tracking.record_signals(sig, [{"ticker": "XYZ", "direction": "ALCISTA", "price": 101.0,
                                         "signal": "MEDIA", "checklist": {}}], "2026-09-03")
    assert len(sig) == 1
    dates = pd.bdate_range("2026-09-02", periods=25).strftime("%Y-%m-%d")
    daily = pd.DataFrame({"date": dates, "ticker": "XYZ", "price": [100 + i for i in range(1, 26)]})
    ev = tracking.evaluate(sig, daily)
    assert ev[0]["r5"] == 5.0 and ev[0]["r20"] == 20.0
    st = {s["group"]: s for s in tracking.summarize(ev)}
    assert st["ENTRADA"]["hit5"] == 100.0 and st["Con catalizador FDA"]["n"] == 1


def test_paper_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("OPSCAN_DATA_DIR", str(tmp_path))
    book = paper.load()
    legs = [{"action": "COMPRAR", "symbol": "A1", "kind": "C", "strike": 100, "expiration": "2026-11-20", "mid": 5.0},
            {"action": "VENDER", "symbol": "A2", "kind": "C", "strike": 110, "expiration": "2026-11-20", "mid": 2.0}]
    t = paper.open_trade(book, "XYZ", "ALCISTA", legs, "2026-10-01", "AUTO")
    assert t["entry_cost"] == 300.0
    paper.mark(book, {"XYZ": {"A1": {"mid": 9.0}, "A2": {"mid": 3.0}}}, {"XYZ": 108}, "2026-10-05")
    paper.manage_auto(book, "2026-10-05")       # act. 17: el Robot decide al cierre
    assert t["pnl"] == 300.0 and t["status"] == "CLOSED" and "objetivo" in t["close_reason"]
    t2 = paper.open_trade(book, "XYZ", "ALCISTA", legs, "2026-10-06", "MANUAL")
    paper.mark(book, {"XYZ": {"A1": {"mid": 1.0}, "A2": {"mid": 0.5}}}, {"XYZ": 95}, "2026-10-07")
    assert t2["status"] == "OPEN"                          # manual: sin stop automatico
    paper.mark(book, {}, {"XYZ": 104}, "2026-11-23")       # vencida: valor intrinseco
    assert t2["status"] == "CLOSED" and t2["exit_value"] == 400.0
    ok, msg = paper.apply_request(book, paper.parse_request("PAPER CERRAR P0002", ""), "2026-11-24")
    assert not ok


def test_paper_parse_request():
    r = paper.parse_request("PAPER ABRIR DYN", 'texto\n```json\n{"ticker":"DYN","direction":"ALCISTA","legs":[]}\n```')
    assert r["op"] == "open" and r["ticker"] == "DYN"
    assert paper.parse_request("PAPER CERRAR P0012", "")["id"] == "P0012"


def test_gex_profile_has_flip():
    p = cboe_payload("_SPX", 6600.0, SESSION, n_exp=6, iv=0.18, base_oi=5000)
    und, cs = options.parse_cboe(p, "SPX")
    for c in cs:   # mas puts por debajo: gamma negativa abajo, positiva arriba
        if c["kind"] == "P" and c["strike"] < 6550:
            c["oi"] *= 4
        if c["kind"] == "C" and c["strike"] > 6650:
            c["oi"] *= 8
    g = options.gex_profile(cs, 6600.0)
    assert g["regime"] in ("positiva", "negativa") and g["profile"] and g["flip"] is not None


def test_dividend_tagging():
    m = {"bull_premium": 1_000_000, "bear_premium": 0, "effective_premium": 1_000_000, "flow_bias": 1.0}
    u = [{"kind": "CALL", "side": "ASK", "direction": "ALCISTA", "premium": 1_000_000, "weight": 1.0,
          "delta": 0.95, "expiration": "2026-10-16", "symbol": "X"}]
    m2, u2, div = pipeline.tag_dividends(m, u, {"ex_div_date": "2026-10-05"}, "2026-10-01")
    assert div and u2[0]["dividend"] and m2["effective_premium"] == 100_000


def test_build_legs_from_grid():
    grid = {"2026-11-20": [
        {"symbol": "C100", "kind": "C", "strike": 100.0, "dte": 50, "expiration": "2026-11-20", "mid": 6.0, "bid": 5.9, "ask": 6.1},
        {"symbol": "C110", "kind": "C", "strike": 110.0, "dte": 50, "expiration": "2026-11-20", "mid": 2.5, "bid": 2.4, "ask": 2.6}]}
    out = scoring.build_legs("ALCISTA", grid, "2026-11-20", 100.5, 9.0, spread=True)
    assert [l["strike"] for l in out["legs"]] == [100.0, 110.0]
    assert out["cost"] == 350.0 and out["max_gain"] == 650.0 and out["breakeven"] == 103.5


def test_parse_ssga():
    rows = [["Fund Name:", "SPDR S&P MIDCAP 400"], ["Ticker Symbol:", "MDY"], [None, None],
            ["Name", "Ticker", "Identifier", "Weight", "Sector"],
            ["CIENA CORP", "CIEN", "171779309", 0.8, "Information Technology"],
            ["US DOLLAR", "CASH_USD", None, 0.1, "-"]]
    d = universe.parse_ssga_holdings(pd.DataFrame(rows))
    assert set(d) == {"CIEN"} and d["CIEN"]["sector"] == "Information Technology"


def test_earnings_event_move():
    hist = {"avg_abs": 5.0, "median_abs": 5.0}
    em = [{"expiration": "2026-10-16", "dte": 10, "em_pct": 4.0},     # antes de resultados: 1.6 var/dia
          {"expiration": "2026-10-23", "dte": 17, "em_pct": 7.0}]     # despues
    r = enrich.earnings_vs_expected(hist, em, "2026-10-20")
    assert r["event_only"] and 4.0 < r["implied_pct"] < 5.0          # sqrt(49 - 1.6*16) ~ 4.83
    assert r["label"] == "en linea"


def _fs(**kw):
    d = {"confirmed": 0, "confirmed_bull_premium": 0, "confirmed_bear_premium": 0, "not_confirmed": 0,
         "pending": 0, "confirmed_list": [], "prev_bull_premium": 0, "prev_bear_premium": 0,
         "sessions_bull": 0, "sessions_bear": 0}
    d.update(kw)
    return d


def _m(**kw):
    d = {"unusual_count": 2, "unusual_premium": 2_000_000, "effective_premium": 2_000_000,
         "bull_premium": 2_000_000, "bear_premium": 0, "flow_bias": 1.0, "whales": 1,
         "call_premium": 3_000_000, "put_premium": 500_000, "_unusual": [], "change_pct": 0.5, "iv30_change": 0.0}
    d.update(kw)
    return d


def test_accumulation_trend_iv_bonus(cfg):
    base = scoring.score_ticker(_m(), {}, _fs(), [], None, None, [], None, cfg)
    tech = {"above20": True, "above50": True, "rel_stock_vol": 2.5}
    boosted = scoring.score_ticker(_m(change_pct=1.5, iv30_change=0.03), {}, _fs(sessions_bull=3), [], None, None, [],
                                   None, cfg, tech)
    c = boosted["components"]
    assert c["acumulacion"] == 7 and c["accion"] == 6 and c["iv_subiendo"] == 3
    assert boosted["score"] == pytest.approx(base["score"] + 16, abs=0.5)


def test_reactive_flow_penalized(cfg):
    calm = scoring.score_ticker(_m(change_pct=0.5), {}, _fs(), [], None, None, [], None, cfg)
    chased = scoring.score_ticker(_m(change_pct=9.0), {}, _fs(), [], None, None, [], None, cfg)
    assert chased["components"]["flujo"] == pytest.approx(calm["components"]["flujo"] * 0.6, abs=0.2)
    assert any("reactivo" in r for r in chased["reasons"])


def test_liquidity_prefers_tight_spread():
    grid = {"2026-11-20": [
        {"symbol": "C100", "kind": "C", "strike": 100.0, "dte": 50, "expiration": "2026-11-20", "mid": 5.0, "bid": 4.0, "ask": 6.0},
        {"symbol": "C102", "kind": "C", "strike": 102.0, "dte": 50, "expiration": "2026-11-20", "mid": 4.0, "bid": 3.9, "ask": 4.1}]}
    out = scoring.build_legs("ALCISTA", grid, "2026-11-20", 100.2, None, spread=False)
    assert out["legs"][0]["strike"] == 102.0 and out["liquidity"] == "buena"


def test_technicals_features():
    from opscan import technicals
    f = technicals.features_from_series([100 + i for i in range(60)], [1000] * 60, today_volume=2500)
    assert f["above20"] and f["above50"] and f["rel_stock_vol"] == 2.5 and f["dist_52w_high"] == 0.0


def test_oi_cero_no_desconfirma_y_sesion_previa():
    import datetime as dt
    import pandas as pd
    from opscan import state
    from opscan.util import last_session_et, ET
    flags = pd.DataFrame([{"date": "2026-10-01", "ticker": "X", "symbol": "S1", "kind": "P", "strike": 1,
                           "expiration": "2026-10-16", "volume": 100, "oi_before": 50, "premium": 1000,
                           "direction": "BAJISTA", "side": "ASK", "status": "PENDIENTE", "oi_after": None,
                           "checked_date": None, "weight": 1.0}], columns=state.FLAG_COLS)
    out = state.confirm_flags(flags, "X", {"S1": 0}, "2026-10-02")
    assert out.iloc[0]["status"] == "PENDIENTE"
    out = state.confirm_flags(flags, "X", {"S1": 200}, "2026-10-02")
    assert out.iloc[0]["status"] == "CONFIRMADA"
    assert last_session_et(dt.datetime(2026, 10, 2, 7, 0, tzinfo=ET)).isoformat() == "2026-10-01"


def test_cartera_con_capital():
    from opscan import paper
    paper.configure({"paper": {"capital": 10000, "max_pct_trade": 5}})
    book = {"trades": [], "seq": 0}
    leg = lambda mid, k=20: [{"symbol": f"X{k}", "kind": "C", "strike": k, "expiration": "2026-11-20",
                              "action": "COMPRAR", "mid": mid}]
    t = paper.open_trade(book, "X", "ALCISTA", leg(1.8), "2026-10-02", "AUTO")
    assert t["contracts"] == 2 and t["entry_cost"] == 360.0          # 500 // 180 = 2
    a = paper.account(book, "AUTO")
    assert paper.account(book, "MANUAL")["cash"] == 10000.0      # carteras separadas
    assert a["cash"] == 9640.0 and a["equity"] == 10000.0
    # AUTO: 1 contrato de $900 supera el maximo de $500 -> no abre
    assert paper.open_trade(book, "Y", "ALCISTA", leg(9.0), "2026-10-02", "AUTO") is None
    # MANUAL: abre 1 contrato aunque supere el maximo, si hay saldo
    ok, msg = paper.apply_request(book, {"op": "open", "ticker": "Y", "direction": "ALCISTA", "legs": leg(9.0)},
                                  "2026-10-02")
    assert ok and "1 contrato" in msg
    # se valora por numero de contratos
    paper.mark(book, {"X": {"X20": {"mid": 3.6}}}, {"X": 21}, "2026-10-05")
    paper.manage_auto(book, "2026-10-05")       # act. 17: el Robot decide al cierre
    assert book["trades"][0]["value"] == 720.0 and book["trades"][0]["pnl_pct"] == 100.0
    assert book["trades"][0]["status"] == "CLOSED"                    # objetivo +100% (AUTO)
    paper.snapshot(book, "2026-10-05")
    assert book["curves"]["AUTO"][-1]["equity"] == paper.account(book, "AUTO")["equity"]
    ex = paper.export(book)
    assert ex["summary"]["account"]["capital"] == 10000.0 and ex["curves"]["MANUAL"]


def test_gestion_y_limites_auto(monkeypatch):
    from opscan import paper
    paper.configure({"paper": {"capital": 10000, "max_pct_trade": 5, "max_new_per_day": 2, "max_open_auto": 8}})
    book = {"trades": [], "seq": 0, "rules_version": paper.RULES_VERSION}
    leg = lambda tk: [{"symbol": f"{tk}C", "kind": "C", "strike": 20, "expiration": "2026-11-20",
                       "action": "COMPRAR", "mid": 1.0}]
    recs = [{"ticker": t, "score": sc, "direction": "ALCISTA", "checklist": {"entrada": True, "flujo_confirmado": True},
             "next_catalyst": {"date": "2026-11-14", "type": "PDUFA"},
             "idea": {"legs": leg(t), "liquidity": liq}}
            for t, sc, liq in (("A", 60, "buena"), ("B", 70, "mala"), ("C", 50, "aceptable"), ("D", 40, "buena"))]
    assert paper.auto_open(book, recs, "2026-10-02") == 2            # max 2/dia, salta B (liquidez mala)
    assert [t["ticker"] for t in book["trades"]] == ["A", "C"]
    assert book["trades"][0]["catalyst"]["date"] == "2026-11-14"
    rmap = {r["ticker"]: r for r in recs}
    assert paper.advise(book, rmap, "2026-10-05") == []
    assert book["trades"][0]["advice"]["action"] == "MANTENER"
    # el catalizador ya paso -> CERRAR
    ch = paper.advise(book, rmap, "2026-11-16")
    assert {t["ticker"] for t in ch} == {"A", "C"} and book["trades"][0]["advice"]["action"] == "CERRAR"
    # las AUTO abiertas con reglas antiguas se anulan al cargar
    old = {"trades": [dict(book["trades"][0], status="OPEN")], "seq": 1}
    import json, os, tempfile
    from opscan import config
    d = tempfile.mkdtemp()
    monkeypatch.setenv("OPSCAN_DATA_DIR", d)
    os.makedirs(config.state_dir(), exist_ok=True)
    json.dump(old, open(os.path.join(config.state_dir(), "paper_trades.json"), "w"))
    assert paper.load()["trades"] == []


def test_cola_de_ordenes(tmp_path, monkeypatch):
    import json
    from opscan import paper
    monkeypatch.setenv("OPSCAN_DATA_DIR", str(tmp_path / "data"))
    paper.configure({"paper": {"capital": 10000, "max_pct_trade": 5}})
    od = str(tmp_path / "orders")
    body = json.dumps({"ticker": "NFLX", "direction": "ALCISTA", "contracts": 2, "nonce": "abc",
                       "legs": [{"symbol": "NFLX261030C00071000", "kind": "C", "strike": 71,
                                 "expiration": "2026-10-30", "action": "COMPRAR", "mid": 2.17}]})
    res = tmp_path / "r.txt"
    paper.main(["--title", "PAPER ABRIR NFLX", "--body-file", _w(tmp_path, body), "--orders-dir", od,
                "--result-file", str(res)])
    assert res.read_text().startswith("OK") and "M0001" in res.read_text()
    prev = json.load(open(f"{od}/preview.json"))
    assert prev["orders_done"] == 1 and prev["order_results"]["abc"]["ok"]
    assert prev["summary"]["account"]["cash"] == 10000 - 434
    # segunda orden: cerrar M0001 -> la vista previa aplica ambas sobre la cartera de la rama data
    paper.main(["--title", "PAPER CERRAR M0001", "--body-file", _w(tmp_path, '{"id": "M0001"}'),
                "--orders-dir", od, "--result-file", str(res)])
    prev = json.load(open(f"{od}/preview.json"))
    assert prev["orders_done"] == 2 and [t["status"] for t in prev["trades"]] == ["CLOSED"]
    # el escaner aplica la cola a la cartera real una sola vez
    book = paper.load()
    paper.apply_orders(book, paper.load_orders(od), "2026-10-02")
    paper.apply_orders(book, paper.load_orders(od), "2026-10-02")
    assert len(book["trades"]) == 1 and book["orders_done"] == 2


def _w(tmp_path, text):
    import uuid
    p = tmp_path / f"b{uuid.uuid4().hex}.txt"
    p.write_text(text)
    return str(p)
