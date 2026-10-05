"""v2.3: compras de directivos (Form 4), probabilidad de ganar."""
import datetime as dt

from opscan import insiders, scoring
from tests.test_units import cfg  # noqa: F401  (fixture)

XML = """<?xml version="1.0"?>
<ownershipDocument><schemaVersion>X0609</schemaVersion><documentType>4</documentType>
<issuer><issuerCik>0000807882</issuerCik><issuerName>JACK IN THE BOX INC</issuerName>
<issuerTradingSymbol>JACK</issuerTradingSymbol></issuer>
<reportingOwner><reportingOwnerId><rptOwnerCik>0002155323</rptOwnerCik><rptOwnerName>Montgomery Taylor K</rptOwnerName></reportingOwnerId>
<reportingOwnerRelationship><isDirector>0</isDirector><isOfficer>1</isOfficer><isTenPercentOwner>0</isTenPercentOwner>
<isOther>0</isOther><officerTitle>EVP President</officerTitle></reportingOwnerRelationship></reportingOwner>
<aff10b5One>0</aff10b5One>
<nonDerivativeTable>
<nonDerivativeTransaction><securityTitle><value>COMMON STOCK</value></securityTitle>
<transactionDate><value>2026-10-01</value></transactionDate>
<transactionCoding><transactionFormType>4</transactionFormType><transactionCode>A</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>106506</value><footnoteId id="F1"/></transactionShares>
<transactionPricePerShare><value>0.00</value></transactionPricePerShare>
<transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts></nonDerivativeTransaction>
<nonDerivativeTransaction><securityTitle><value>COMMON STOCK</value></securityTitle>
<transactionDate><value>2026-10-02</value></transactionDate>
<transactionCoding><transactionFormType>4</transactionFormType><transactionCode>P</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>5,000</value></transactionShares>
<transactionPricePerShare><value>40.50</value></transactionPricePerShare>
<transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts></nonDerivativeTransaction>
</nonDerivativeTable></ownershipDocument>"""


def test_parse_form4_real_format():
    f = insiders.parse_form4(XML)
    assert f["ticker"] == "JACK" and f["issuer_cik"] == "807882" and f["is_officer"]
    assert f["roles"] == ["EVP President"]
    p = [t for t in f["tx"] if t["code"] == "P"][0]
    assert p["shares"] == 5000 and p["price"] == 40.5 and p["value"] == 202500


def _tx(owner, value, date, code="P"):
    return {"ticker": "XYZ", "date": date, "code": code, "owner": owner, "roles": ["CEO"], "officer": True,
            "director": False, "value": value, "shares": 1, "price": value, "adsh": owner + date}


def test_summarize_and_score():
    today = dt.date(2026, 10, 5)
    cache = {"tx": [_tx("ANA", 300000, "2026-09-20"), _tx("LUIS", 150000, "2026-09-28"),
                    _tx("OLD", 900000, "2026-07-01"), _tx("ANA", 5000, "2026-09-30"),
                    _tx("PEDRO", 2000000, "2026-09-25", "S"),
                    dict(_tx("FONDO", 5000000, "2026-09-26"), officer=False, director=False)]}
    s = insiders.summarize(cache, "XYZ", today)
    assert s["buys"] == 2 and s["buyers"] == 2 and s["buy_usd"] == 450000     # la antigua y la pequena no cuentan
    assert s["sells"] == 1 and s["sell_usd"] == 2000000
    p, why = insiders.score(s)
    assert p == 7 and "2 directivo" in why
    assert insiders.summarize(cache, "AAA", today) is None


def test_search_day_paginates(monkeypatch):
    pages = []

    def fake(url, params=None, headers=None, timeout=None):
        pages.append(params["from"])
        n = 100 if params["from"] < 200 else 30
        return {"hits": {"total": {"value": 230}, "hits": [
            {"_id": f"0001-26-{params['from'] + i:06d}:form4.xml",
             "_source": {"ciks": ["0002155323", "0000807882"], "form": "4"}} for i in range(n)]}}
    monkeypatch.setattr(insiders, "get_json", fake)
    rows = insiders.search_day("2026-10-02", pause=0)
    assert pages == [0, 100, 200] and len(rows) == 230 and rows[0][0] == ["2155323", "807882"]


def test_build_uses_universe_only(monkeypatch):
    monkeypatch.setattr(insiders, "load_cik_map", lambda: {"JACK": "807882", "AAPL": "320193"})
    monkeypatch.setattr(insiders, "search_day", lambda day, pause=0: [(["2155323", "807882"], "0002155323-26-000007", "form4.xml"),
                                                                      (["999", "123"], "0000999-26-1", "x.xml")])

    class R:
        text = XML
    calls = []
    monkeypatch.setattr(insiders, "get", lambda url, **kw: (calls.append(url), R())[1])
    c = insiders.build({"JACK", "AAPL"}, {}, days=2, pause=0, today=dt.date(2026, 10, 5))
    assert len(calls) == 1 and "/807882/000215532326000007/form4.xml" in calls[0]
    assert [t["code"] for t in c["tx"]] == ["P"] and c["tx"][0]["ticker"] == "JACK"
    c2 = insiders.build({"JACK", "AAPL"}, c, days=2, pause=0, today=dt.date(2026, 10, 5))
    assert len(calls) == 1 and len(c2["tx"]) == 1                     # no repite lo ya leido


def test_scoring_directivos_checklist(cfg):
    from tests.test_units import cboe_payload, SESSION
    from opscan import options
    p = cboe_payload("XYZ", 100.0, SESSION, unusual=[
        {"kind": "C", "strike": 110, "exp_idx": 6, "volume": 6000, "oi": 100, "side": "ASK"}])
    und, cs = options.parse_cboe(p, "XYZ")
    m, unusual = options.analyze_chain(und, cs, cfg["options"])
    m["_unusual"] = unusual
    fs = {"confirmed": 2, "confirmed_bull_premium": 1_500_000, "confirmed_bear_premium": 0,
          "not_confirmed": 0, "pending": 0, "confirmed_list": []}
    f4 = {"window_days": 30, "buys": 3, "buyers": 3, "buy_usd": 1_200_000, "officer_buy": True,
          "top_buyers": [{"owner": "ANA", "roles": ["CEO"], "value": 1e6}]}
    s = scoring.score_ticker(m, {"rel_vol": 3}, fs, [], None, None, [], {"form4": f4}, cfg)
    assert s["components"]["directivos"] == 10 and s["checklist"]["directivos"]
    assert s["checklist"]["puntos"] == 3 and s["checklist"]["entrada"]          # directivos + flujo confirmado


def test_semaforo_y_cambios():
    from opscan import market
    recs = [{"ticker": "VIX", "price": 27.0, "change_pct": 5.0, "signal": "BAJA"},
            {"ticker": "AAA", "call_vol": 1000, "put_vol": 1300, "signal": "MEDIA"}]
    s = market.semaforo(recs, {"regime": "negativa"})
    assert s["level"] == "rojo" and s["pc_ratio"] == 1.3
    assert market.semaforo([{"ticker": "VIX", "price": 14, "change_pct": -2}], {"regime": "positiva"})["level"] == "verde"

    def rec(tk, sc, sig, d, ent=False, conf=False):
        return {"ticker": tk, "score": sc, "signal": sig, "direction": d,
                "checklist": {"entrada": ent, "flujo_confirmado": conf}}
    c = market.roll({}, "2026-10-01", [rec("A", 50, "MEDIA", "ALCISTA", True, True), rec("B", 40, "MEDIA", "ALCISTA")])
    assert market.changes(c)["since"] is None
    c = market.roll(c, "2026-10-01", [rec("A", 50, "MEDIA", "ALCISTA", True, True), rec("B", 40, "MEDIA", "ALCISTA")])
    c = market.roll(c, "2026-10-02", [rec("A", 30, "MEDIA", "ALCISTA"), rec("B", 60, "ALTA", "BAJISTA", True, True)])
    ch = market.changes(c)
    assert ch["since"] == "2026-10-01"
    assert [x["ticker"] for x in ch["new_entries"]] == ["B"] and [x["ticker"] for x in ch["lost_entries"]] == ["A"]
    assert ch["flips"][0]["ticker"] == "B" and ch["new_alta"][0]["ticker"] == "B" and ch["confirmed"][0]["ticker"] == "B"


def test_split_score_y_diario():
    from opscan import market, paper
    sp = market.split_score({"score": 60, "components": {"flujo": 20}}, 1_000_000, 0, {"bull_premium": 500_000})
    assert sp == {"confirmed": 50.0, "provisional": 10.0}
    paper.configure({})
    book = {"trades": [], "seq": 0}
    leg = [{"symbol": "X1", "kind": "C", "strike": 10, "expiration": "2026-11-20", "action": "COMPRAR", "mid": 1, "iv": 0.5}]
    ok, _ = paper.apply_request(book, {"op": "open", "ticker": "X", "direction": "ALCISTA", "legs": leg,
                                       "why": "flujo confirmado + FDA", "trade_id": "M0009"}, "2026-10-05")
    t = book["trades"][0]
    assert ok and t["why"] == "flujo confirmado + FDA" and t["legs"][0]["iv"] == 0.5
    req = paper.parse_request("PAPER CERRAR M0009", '{"id":"M0009","lesson":"no esperar al evento"}')
    ok, _ = paper.apply_request(book, req, "2026-10-06")
    assert ok and t["lesson"] == "no esperar al evento" and t["status"] == "CLOSED"


def test_informe_semanal():
    from opscan import market
    out = {"curves": {"MANUAL": [{"date": "2026-09-28", "equity": 10000}, {"date": "2026-10-05", "equity": 10150}]},
           "summary": {"account": {"return_pct": 1.5}},
           "trades": [{"status": "OPEN", "source": "MANUAL", "ticker": "SMMT", "pnl_pct": 12, "advice": {"action": "MANTENER"},
                       "catalyst": {"type": "PDUFA", "date": "2026-10-09"}, "expiration": "2026-11-20"}]}
    txt = market.weekly_report(out, [{"ticker": "SPY", "tech": {"chg_5d": 0.8}}], dt.date(2026, 10, 5))
    assert "+1.50% esta semana" in txt and "SPY" in txt and "PDUFA el 2026-10-09" in txt
