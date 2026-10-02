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
