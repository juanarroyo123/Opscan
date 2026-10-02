import datetime as dt
import os

import pandas as pd
import pytest
import yaml

from opscan import catalysts, congress, futures, options, scoring, state
from opscan.config import load_config
from tests.fakes import cboe_payload, cot_records, occ

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
SESSION = dt.date(2026, 9, 30)


@pytest.fixture
def cfg():
    return load_config()


# ------------------------------------------------------------------ opciones
def test_parse_occ():
    assert options.parse_occ("AAPL260116C00150000") == ("AAPL", dt.date(2026, 1, 16), "C", 150.0)
    assert options.parse_occ("SPXW261218P05500000")[3] == 5500.0
    assert options.parse_occ("BRK.B261218P00400500")[3] == 400.5
    assert options.parse_occ("basura") is None


def test_cboe_symbol():
    assert options.cboe_symbol("SPX") == "_SPX"
    assert options.cboe_symbol("AAPL") == "AAPL"
    assert options.yf_symbol("BRK.B") == "BRK-B"
    assert options.yf_symbol("VIX") == "^VIX"


def test_parse_cboe_all_expirations():
    p = cboe_payload("NVDA", 180.0, SESSION, n_exp=10)
    und, cs = options.parse_cboe(p, "NVDA")
    assert und["price"] == 180.0
    assert und["session_date"] == "2026-09-30"
    assert abs(und["iv30"] - 0.35) < 1e-9
    assert len({c["expiration"] for c in cs}) == 10   # todos los vencimientos
    assert all(c["dte"] >= 0 for c in cs)


def test_side_estimation():
    c = {"bid": 1.0, "ask": 1.2, "last": 1.2}
    assert options.estimate_side(c) == "ASK"
    c["last"] = 1.0
    assert options.estimate_side(c) == "BID"
    c["last"] = 1.1
    assert options.estimate_side(c) == "MID"
    assert options.estimate_side({"bid": 0, "ask": 0, "last": 1}) == "MID"


def test_analyze_chain_detects_unusual_bullish(cfg):
    p = cboe_payload("XYZ", 100.0, SESSION, unusual=[
        {"kind": "C", "strike": 110, "exp_idx": 4, "volume": 5000, "oi": 300, "side": "ASK"},
        {"kind": "C", "strike": 115, "exp_idx": 5, "volume": 3000, "oi": 100, "side": "ASK"},
    ])
    und, cs = options.parse_cboe(p, "XYZ")
    m, unusual = options.analyze_chain(und, cs, cfg["options"])
    assert m["unusual_count"] == 2
    assert all(u["direction"] == "ALCISTA" and u["side"] == "ASK" and u["otm"] for u in unusual)
    assert m["flow_bias"] > 0.9
    assert m["bull_premium"] > m["bear_premium"]
    assert m["n_expirations"] == 8
    assert 0.3 < m["iv30"] < 0.4
    assert m["max_pain"] is not None and m["call_wall"] is not None
    assert m["expected_moves"] and m["expected_moves"][0]["em_pct"] > 0
    assert m["gex_usd_1pct"] is not None


def test_bearish_put_buying(cfg):
    p = cboe_payload("XYZ", 100.0, SESSION, unusual=[
        {"kind": "P", "strike": 90, "exp_idx": 3, "volume": 8000, "oi": 200, "side": "ASK"}])
    und, cs = options.parse_cboe(p, "XYZ")
    m, unusual = options.analyze_chain(und, cs, cfg["options"])
    assert unusual[0]["direction"] == "BAJISTA"
    assert m["flow_bias"] < -0.9


def test_low_volume_not_unusual(cfg):
    p = cboe_payload("XYZ", 100.0, SESSION)   # volumen 40, OI 800 en todo
    und, cs = options.parse_cboe(p, "XYZ")
    m, unusual = options.analyze_chain(und, cs, cfg["options"])
    assert m["unusual_count"] == 0 and m["flow_bias"] == 0


def test_deep_itm_marked_hedge(cfg):
    p = cboe_payload("XYZ", 100.0, SESSION, unusual=[
        {"kind": "C", "strike": 50, "exp_idx": 6, "volume": 3000, "oi": 10, "side": "ASK"}])
    und, cs = options.parse_cboe(p, "XYZ")
    _, unusual = options.analyze_chain(und, cs, cfg["options"])
    assert unusual and unusual[0]["hedge_like"]


def test_iv30_interpolation():
    term = [(9, "a", 0.30), (44, "b", 0.40)]
    v = options.interp_iv30(term)
    assert 0.30 < v < 0.40
    assert options.interp_iv30([(2, "x", 0.9)]) is None   # ignora vencimientos < 5 dias


def test_bs_greeks():
    d, g = options.bs_greeks("C", 100, 100, 0.25, 0.3)
    assert 0.5 < d < 0.6 and g > 0
    d, _ = options.bs_greeks("P", 100, 100, 0.25, 0.3)
    assert -0.5 < d < -0.4


# ------------------------------------------------------------------ estado / confirmacion OI
def test_oi_confirmation_flow(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("OPSCAN_DATA_DIR", str(tmp_path))
    exp = SESSION + dt.timedelta(days=30)
    sym = occ("XYZ", exp, "C", 110)
    unusual = [{"ticker": "XYZ", "symbol": sym, "kind": "CALL", "strike": 110, "expiration": exp.isoformat(),
                "volume": 5000, "oi": 300, "premium": 900000, "direction": "ALCISTA", "side": "ASK"}]
    flags = state.add_flags(state.load_flags(), unusual, "2026-09-29")
    # misma sesion: no se evalua
    f2 = state.confirm_flags(flags, "XYZ", {sym: 300}, "2026-09-29")
    assert f2.iloc[0]["status"] == "PENDIENTE"
    # sesion siguiente: OI sube 4000 >= 50% de 5000 -> confirmada
    f3 = state.confirm_flags(flags, "XYZ", {sym: 4300}, "2026-09-30")
    assert f3.iloc[0]["status"] == "CONFIRMADA"
    s = state.flag_summary(f3, "XYZ", "2026-09-30")
    assert s["confirmed"] == 1 and s["confirmed_bull_premium"] == 900000
    # OI no sube -> no confirmada
    f4 = state.confirm_flags(flags, "XYZ", {sym: 350}, "2026-09-30")
    assert f4.iloc[0]["status"] == "NO CONFIRMADA"
    state.save_flags(f3)
    assert state.load_flags().iloc[0]["status"] == "CONFIRMADA"


def test_baselines_rel_vol_and_iv_rank():
    rows = [{"date": (SESSION - dt.timedelta(days=30 - i)).isoformat(), "ticker": "XYZ", "opt_vol": 1000,
             "iv30": 0.20 + i * 0.01} for i in range(30)]
    df = pd.DataFrame(rows)
    b = state.baselines(df, "XYZ", SESSION.isoformat(), 4000, 0.60)
    assert b["rel_vol"] == 4.0
    assert b["iv_rank"] == 100.0
    b2 = state.baselines(df.head(3), "XYZ", SESSION.isoformat(), 4000, 0.3)
    assert b2["rel_vol"] is None and b2["iv_rank"] is None   # sin historia suficiente


# ------------------------------------------------------------------ futuros
def test_cot_analysis_and_quadrant():
    cot = futures.analyze_cot(cot_records("13874A"), cot_records("13874A"))
    assert cot["report_date"] == "2026-09-22"
    assert cot["cot_index_spec"] == 100.0       # especuladores en maximo de 3 anos
    assert cot["lev_money_index"] is not None
    px = {"cot_week_chg": 1.5, "above_sma50": True}
    cls = futures.classify({}, cot, px)
    assert cls["quadrant"]["name"] in ("Entrada de largos", "Cierre de cortos")
    assert any("muy largos" in r for r in cls["reasons"])


def test_quadrants_all():
    for (ps, os_), (name, _, b) in futures.QUADRANTS.items():
        cot = {"oi_change": 100 * os_, "cot_index_spec": 50, "cot_index_comm": 50}
        cls = futures.classify({}, cot, {"cot_week_chg": ps * 1.0})
        assert cls["quadrant"]["name"] == name


def test_price_stats():
    idx = pd.date_range("2026-01-01", periods=260, freq="B")
    close = pd.Series([100 + i * 0.1 for i in range(260)], index=idx)
    vol = pd.Series([1000] * 259 + [3000], index=idx)
    px = futures.price_stats(close, vol, idx[-1].strftime("%Y-%m-%d"), idx[-6].strftime("%Y-%m-%d"))
    assert px["above_sma50"] is True and px["vol_rel"] == 3.0
    assert "volumen alto" in px["daily_pattern"]
    assert px["cot_week_chg"] > 0


def test_sector_context():
    fut = {"markets": [{"key": "CL", "name": "Crudo", "bias": 0.8, "label": "ALCISTA"},
                       {"key": "ZN", "name": "T-Note", "bias": 0.6, "label": "ALCISTA"}],
           "regime": {"bias": 0.4, "label": "ALCISTA"}}
    b, why = futures.sector_context(fut, "Energy", {"Energy": ["CL"]})
    assert b == 0.6 and len(why) == 2
    b2, _ = futures.sector_context(fut, "Financials", {"Financials": ["-ZN"]})
    assert b2 == -0.1


# ------------------------------------------------------------------ congreso (datos reales de muestra)
def _member_idx():
    leg = yaml.safe_load(open(os.path.join(FIX, "legislators-current.yaml")))
    mem = yaml.safe_load(open(os.path.join(FIX, "committee-membership-current.yaml")))
    com = yaml.safe_load(open(os.path.join(FIX, "committees-current.yaml")))
    return congress.build_member_index(leg, mem, com)


def test_member_index_and_committees():
    idx = _member_idx()
    assert len(idx) > 500
    tub = idx.get(("tuberville", "AL", "senate"))
    assert tub and any("Armed Services" in c for c in tub["committees"])
    assert "Industrials" in tub["sectors"]


def test_normalize_real_kadoa_trades():
    import json
    raw = json.load(open(os.path.join(FIX, "kadoa_trades.json")))
    trades = congress.normalize_trades(raw, _member_idx(), True, 3650, today=dt.date(2026, 10, 1))
    assert len(trades) > 300
    assert {t["chamber"] for t in trades} >= {"Camara", "Senado", "Ejecutivo"}
    assert sum(1 for t in trades if t["committees"]) > 50   # cruce con comites funciona
    assert all(t["direction"] in ("Compra", "Venta", "Canje", "Otro") for t in trades)


def test_summarize_cluster_and_committee():
    t = lambda m, d, days, secs: {"ticker": "LMT", "member": m, "direction": d, "amount_mid": 8000,
                                  "days": days, "transaction_date": "2026-09-01",
                                  "committee_sectors": secs, "is_option": False}
    trades = [t("A", "Compra", 10, ["Industrials"]), t("B", "Compra", 20, []), t("C", "Venta", 40, [])]
    s = congress.summarize_by_ticker(trades, {"LMT": "Industrials"})["LMT"]
    assert s["n_buyers"] == 2 and s["cluster"] and s["committee_relevant"]
    assert s["bias"] > 0


def test_last_name():
    assert congress.last_name("Thomas H. Kean Jr") == "kean"
    assert congress.last_name("April McClain Delaney") == "delaney"


# ------------------------------------------------------------------ catalizadores
def test_pdufa_bio_parse():
    p = {"meta": {"total": 2, "returned": 2}, "data": [
        {"ticker": "RCKT", "company": "Rocket", "date": "2026-10-20", "name": "Kresladi", "type": "PDUFA",
         "status": "Pending", "indication": "LAD-I", "url": "https://www.pdufa.bio/x", "cash_runway_months": 14},
        {"ticker": "GSK", "company": "GSK", "date": "2026-06-18", "type": "PDUFA", "status": "Decided"}]}
    r = catalysts.parse_pdufa_bio(p, "pdufa")
    assert len(r) == 1 and r[0]["ticker"] == "RCKT" and r[0]["type"] == "PDUFA"
    assert r[0]["extra"]["cash_runway_months"] == 14


def test_finnhub_parse():
    r = catalysts.parse_finnhub_earnings({"earningsCalendar": [
        {"date": "2026-10-28", "symbol": "msft", "hour": "amc", "epsEstimate": 3.1}]})
    assert r[0]["ticker"] == "MSFT" and "cierre" in r[0]["title"]


def test_fomc_html_parse():
    html = """<h4>2027 FOMC Meetings</h4><div>January</div><div>26-27</div><div>March</div><div>16-17*</div>
              <div>April/May</div><div>30-1</div>"""
    d = catalysts.parse_fomc_html(html)
    assert "2027-01-27" in d and "2027-03-17" in d and "2027-05-01" in d


def test_opex_and_finalize():
    ev = catalysts.opex_events(60, today=dt.date(2026, 10, 1))
    dates = [e["date"] for e in ev if e["ticker"] == ""]
    assert "2026-10-16" in dates and "2026-11-20" in dates
    fin = catalysts.finalize(ev + ev, 60, today=dt.date(2026, 10, 1))
    assert len(fin) == len({(e["date"], e["title"]) for e in fin})   # sin duplicados
    assert all(-2 <= e["days"] <= 60 for e in fin)


def test_clinicaltrials_sponsor_to_ticker():
    payload = {"studies": [{"protocolSection": {
        "identificationModule": {"nctId": "NCT1", "briefTitle": "Study"},
        "statusModule": {"primaryCompletionDateStruct": {"date": "2026-11"}},
        "designModule": {"phases": ["PHASE3"]},
        "sponsorCollaboratorsModule": {"leadSponsor": {"name": "Vertex Pharmaceuticals Incorporated"}}}}]}
    nm = {catalysts.norm_company("VERTEX PHARMACEUTICALS INC / MA"): "VRTX",
          catalysts.norm_company("Vertex Pharmaceuticals Inc"): "VRTX"}
    r = catalysts.parse_clinicaltrials(payload, {"PHASE3"}, nm)
    assert r[0]["ticker"] == "VRTX" and r[0]["date"] == "2026-11-01"


# ------------------------------------------------------------------ scoring
def test_score_entry_checklist(cfg):
    p = cboe_payload("XYZ", 100.0, SESSION, unusual=[
        {"kind": "C", "strike": 110, "exp_idx": 6, "volume": 6000, "oi": 100, "side": "ASK"}])
    und, cs = options.parse_cboe(p, "XYZ")
    m, unusual = options.analyze_chain(und, cs, cfg["options"])
    m["_unusual"] = unusual
    b = {"rel_vol": 3.2, "iv_rank": 70}
    fs = {"confirmed": 2, "confirmed_bull_premium": 1_500_000, "confirmed_bear_premium": 0,
          "not_confirmed": 0, "pending": 0, "confirmed_list": []}
    cats = [{"date": (SESSION + dt.timedelta(days=20)).isoformat(), "days": 20, "type": "PDUFA"}]
    cong = {"n_buyers": 2, "n_sellers": 0, "cluster": True, "committee_relevant": True, "bias": 1.0}
    s = scoring.score_ticker(m, b, fs, cats, cong, 0.5, ["Indices alcista"], None, cfg)
    assert s["direction"] == "ALCISTA"
    assert s["checklist"]["puntos"] == 4 and s["checklist"]["entrada"]
    assert s["signal"] == "ALTA"
    assert s["idea"]["structure"].startswith("Call debit spread")   # IV rank alto -> spread
    assert s["idea"]["dte"] >= 27                                    # vence despues del catalizador


def test_score_no_flow_no_entry(cfg):
    m = {"unusual_count": 0, "unusual_premium": 0, "flow_bias": 0, "whales": 0, "_unusual": []}
    fs = {"confirmed": 0, "confirmed_bull_premium": 0, "confirmed_bear_premium": 0,
          "not_confirmed": 0, "pending": 0, "confirmed_list": []}
    s = scoring.score_ticker(m, {}, fs, [], None, None, [], None, cfg)
    assert s["score"] == 0 and not s["checklist"]["entrada"] and s["direction"] == "MIXTO"


# ------------------------------------------------------------------ alertas y utilidades
def test_alerts_send_once(cfg, monkeypatch):
    from opscan import alerts, util
    sent_msgs = []

    class S:
        def post(self, url, json=None, timeout=None):
            sent_msgs.append(json["text"])
            return type("R", (), {"status_code": 200})()
    monkeypatch.setattr(alerts, "session", lambda: S())
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    r = {"ticker": "RCKT", "direction": "ALCISTA", "score": 70, "signal": "ALTA", "reasons": ["a_b *c*"],
         "checklist": {"puntos": 3, "congreso": True, "futuros": False, "flujo_confirmado": True, "entrada": True},
         "idea": {"structure": "Call larga", "expiration": "2026-11-20", "strikes": "call ~3"}}
    st = util.Status()
    sent = alerts.process([r], cfg, {}, "2026-09-30", st)
    sent = alerts.process([r], cfg, sent, "2026-09-30", st)   # no repite
    assert len(sent_msgs) == 1 and "RCKT" in sent_msgs[0]


def test_write_json_cleans_nan(tmp_path):
    from opscan import util
    p = tmp_path / "x.json"
    util.write_json(str(p), {"a": float("nan"), "b": [1.0, float("inf")], "d": dt.date(2026, 1, 2)})
    import json
    assert json.load(open(p)) == {"a": None, "b": [1.0, None], "d": "2026-01-02"}


def test_config_defaults_merge(tmp_path):
    p = tmp_path / "c.yml"
    p.write_text("options:\n  min_premium: 1234\nuniverse:\n  watchlist: [abc]\n")
    c = load_config(str(p))
    assert c["options"]["min_premium"] == 1234 and c["options"]["min_volume"] == 100
    assert c["universe"]["watchlist"] == ["ABC"]


def test_real_cboe_records_parse(cfg):
    """Contratos reales de Cboe (formato verificado el 01/10/2026); subyacente de ejemplo."""
    import json
    p = json.load(open(os.path.join(FIX, "cboe_real_sample.json")))
    und, cs = options.parse_cboe(p, "AAPL")
    assert und["iv30"] == pytest.approx(0.85833) and und["session_date"] == "2026-09-22"
    assert len(cs) == 2 and cs[0]["kind"] == "C" and cs[0]["strike"] == 245.0 and cs[0]["dte"] == 1
    m, unusual = options.analyze_chain(und, cs, cfg["options"])
    assert m["unusual_count"] == 0 and m["call_vol"] == 10 and m["put_vol"] == 2


def test_combo_and_mid_neutral(cfg):
    p = cboe_payload("XYZ", 100.0, SESSION, unusual=[
        {"kind": "C", "strike": 110, "exp_idx": 6, "volume": 5000, "oi": 100, "side": "ASK"},
        {"kind": "P", "strike": 90, "exp_idx": 6, "volume": 5100, "oi": 100, "side": "ASK"}])
    und, cs = options.parse_cboe(p, "XYZ")
    m, unusual = options.analyze_chain(und, cs, cfg["options"])
    assert all(u["combo"] for u in unusual)           # straddle/strangle detectado
    c = {"bid": 1.0, "ask": 1.2, "last": 1.1}
    assert options.DIR_WEIGHTS[("C", options.estimate_side(c))] == ("NEUTRAL", 0.0)


def test_managed_filers_ignored():
    t = lambda m, d, days: {"ticker": "MSFT", "member": m, "direction": d, "amount_mid": 8000, "days": days,
                            "transaction_date": "2026-09-01", "committee_sectors": [], "is_option": False}
    trades = [t("Robot", "Compra", 5) for _ in range(70)] + [t("Ana", "Venta", 10)]
    s = congress.summarize_by_ticker(trades, {}, max_filer_trades=60)["MSFT"]
    assert s["n_buyers"] == 0 and s["n_sellers"] == 1


def test_cot_roll_distortion():
    recs = cot_records("13874A")
    recs[-1]["change_in_open_interest_all"] = "-600000"
    cot = futures.analyze_cot(recs)
    assert cot["roll_distorted"]
    cls = futures.classify({}, cot, {"cot_week_chg": 1.0})
    assert cls["quadrant"]["name"].startswith("Roll")


def test_flow_points_not_saturated_for_megacaps():
    mega = {"unusual_premium": 20_000_000, "call_premium": 300_000_000, "put_premium": 200_000_000,
            "flow_bias": 0.1, "whales": 0}
    small = {"unusual_premium": 2_000_000, "call_premium": 2_500_000, "put_premium": 500_000,
             "flow_bias": 0.9, "whales": 1}
    assert scoring.flow_points(small) > 20 > scoring.flow_points(mega)


def test_same_side_same_kind_not_combo(cfg):
    p = cboe_payload("XYZ", 100.0, SESSION, unusual=[
        {"kind": "C", "strike": 110, "exp_idx": 6, "volume": 5000, "oi": 100, "side": "ASK"},
        {"kind": "C", "strike": 115, "exp_idx": 6, "volume": 5100, "oi": 100, "side": "ASK"}])
    und, cs = options.parse_cboe(p, "XYZ")
    m, unusual = options.analyze_chain(und, cs, cfg["options"])
    assert not any(u["combo"] for u in unusual)       # dos compras de calls: no es spread
    assert m["effective_premium"] == m["unusual_premium"]


def test_hedge_flags_do_not_confirm(cfg):
    exp = (SESSION + dt.timedelta(days=30)).isoformat()
    u = {"ticker": "XYZ", "symbol": "S1", "kind": "CALL", "strike": 50, "expiration": exp, "volume": 1000,
         "oi": 10, "premium": 900000, "direction": "ALCISTA", "side": "ASK", "weight": 0.3}
    f = state.add_flags(state.load_flags(), [u], "2026-09-29")
    f = state.confirm_flags(f, "XYZ", {"S1": 1010}, "2026-09-30")
    s = state.flag_summary(f, "XYZ", "2026-09-30")
    assert s["confirmed"] == 0 and s["confirmed_bull_premium"] == 0


def test_strongest_catalyst_wins(cfg):
    m = {"unusual_count": 0, "unusual_premium": 0, "flow_bias": 0, "whales": 0, "_unusual": []}
    fs = {"confirmed": 0, "confirmed_bull_premium": 0, "confirmed_bear_premium": 0,
          "not_confirmed": 0, "pending": 0, "confirmed_list": []}
    cats = [{"date": "2026-10-19", "days": 17, "type": "Resultados"},
            {"date": "2026-11-14", "days": 43, "type": "PDUFA"}]
    s = scoring.score_ticker(m, {}, fs, cats, None, None, [], None, cfg)
    assert s["next_catalyst"]["type"] == "PDUFA"
