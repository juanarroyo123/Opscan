"""Prueba de extremo a extremo SIN red: todas las fuentes simuladas con datos realistas.
Ejecuta el pipeline dos sesiones seguidas para verificar la confirmacion por OI."""
import datetime as dt
import json
import os
import re

import pandas as pd
import pytest

from opscan import enrich, futures, pipeline, util
from tests.fakes import cboe_payload, cot_records, occ

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
ROOT = os.path.dirname(os.path.dirname(__file__))

SPOTS = {"RCKT": 3.0, "NVDA": 180.0, "SPY": 660.0, "_SPX": 6600.0, "AAPL": 250.0, "XOM": 115.0,
         "DYN": 9.0, "CADL": 6.0, "LMT": 480.0}


class FakeResp:
    def __init__(self, status=200, body=None, text=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else (json.dumps(body) if body is not None else "")
        self.headers = {}

    def json(self):
        return self._body if self._body is not None else json.loads(self.text)


class FakeSession:
    def __init__(self, session_date, oi_bump=False, cboe_fail=()):
        self.session_date = session_date
        self.oi_bump = oi_bump
        self.cboe_fail = set(cboe_fail)
        self.calls = []

    def _unusual(self, tk, spot):
        if tk == "NVDA":
            return [{"kind": "C", "strike": 200, "exp_idx": 5, "volume": 9000, "oi": 400, "side": "ASK"}]
        if tk == "RCKT":
            return [{"kind": "C", "strike": 4, "exp_idx": 6, "volume": 20000, "oi": 900, "side": "ASK"}]
        if tk == "XOM":
            return [{"kind": "P", "strike": 105, "exp_idx": 4, "volume": 7000, "oi": 300, "side": "ASK"}]
        return []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(url)
        if "delayed_quotes/options" in url:
            sym = url.rsplit("/", 1)[-1].replace(".json", "")
            tk = sym.lstrip("_")
            if tk in self.cboe_fail:
                return FakeResp(403)
            spot = SPOTS.get(sym, 50.0 + (hash(sym) % 200))
            unusual = self._unusual(tk, spot)
            oi_over = {}
            if self.oi_bump:   # dia 2: el OI de lo marcado ayer sube => apertura confirmada
                sess = self.session_date
                exps = [sess - dt.timedelta(days=1) + dt.timedelta(days=d) for d in (2, 9, 16, 23, 30, 44, 72, 107)]
                for u in unusual:
                    oi_over[occ(tk, exps[u["exp_idx"]], u["kind"], u["strike"])] = u["oi"] + u["volume"] * 0.9
                unusual = []
            iv = 1.0 if spot < 20 else 0.35
            p = cboe_payload(sym, spot, self.session_date if not self.oi_bump else self.session_date - dt.timedelta(days=1),
                             unusual=unusual, oi_override=oi_over, iv=iv)
            p["data"]["last_trade_time"] = f"{self.session_date}T15:59:59"
            p["timestamp"] = f"{self.session_date} 16:15:00"
            return FakeResp(200, p)
        if "kadoa-org" in url:
            raw = json.load(open(os.path.join(FIX, "kadoa_trades.json")))
            today = dt.date.today()
            raw.append({"id": "t1", "ticker": "LMT", "transaction_date": (today - dt.timedelta(days=5)).isoformat(),
                        "filing_date": today.isoformat(), "filer_name": "Thomas H Tuberville", "branch": "congress",
                        "chamber": "senate", "state": "AL", "party": "R", "transaction_type": "Purchase",
                        "amount_range_low": 15001, "amount_range_high": 50000, "amount_range_label": "$15,001 - $50,000"})
            raw.append({"id": "t2", "ticker": "LMT", "transaction_date": (today - dt.timedelta(days=8)).isoformat(),
                        "filing_date": today.isoformat(), "filer_name": "John Boozman", "branch": "congress",
                        "chamber": "senate", "state": "AR", "party": "R", "transaction_type": "Purchase",
                        "amount_range_low": 1001, "amount_range_high": 15000, "amount_range_label": "$1,001 - $15,000"})
            for who, st in (("Thomas H Tuberville", "AL"), ("John Boozman", "AR")):
                raw.append({"id": "r" + who, "ticker": "RCKT", "transaction_date": (today - dt.timedelta(days=12)).isoformat(),
                            "filing_date": today.isoformat(), "filer_name": who, "branch": "congress",
                            "chamber": "senate", "state": st, "party": "R", "transaction_type": "Purchase",
                            "amount_range_low": 1001, "amount_range_high": 15000, "amount_range_label": "$1,001 - $15,000"})
            return FakeResp(200, raw)
        m = re.search(r"congress-legislators/main/(.+\.yaml)", url)
        if m:
            return FakeResp(200, text=open(os.path.join(FIX, m.group(1))).read())
        if "publicreporting.cftc.gov" in url:
            codes = re.findall(r"'([0-9A-Z]{6})'", params["$where"])
            out = []
            for c in codes:
                out += cot_records(c)
            return FakeResp(200, out)
        if "pdufa.bio" in url:
            kind = url.rsplit("/", 1)[-1]
            d = (dt.date.today() + dt.timedelta(days=20)).isoformat()
            data = [{"ticker": "RCKT", "company": "Rocket Pharmaceuticals", "date": d, "name": "Kresladi",
                     "type": "PDUFA", "status": "Pending", "indication": "LAD-I"}] if kind == "pdufa" else []
            return FakeResp(200, {"meta": {"total": len(data), "returned": len(data)}, "data": data})
        if "clinicaltrials.gov" in url:
            return FakeResp(200, {"studies": []})
        if "company_tickers.json" in url:
            return FakeResp(200, {"0": {"cik_str": 1, "ticker": "VRTX", "title": "VERTEX PHARMACEUTICALS INC"}})
        if "efts.sec.gov" in url:
            return FakeResp(200, {"hits": {"hits": []}})
        if "federalreserve.gov" in url:
            return FakeResp(200, text="<h4>2026 FOMC Meetings</h4><p>October</p><p>27-28</p>")
        if "s-and-p-500-companies" in url:
            return FakeResp(200, text=open(os.path.join(ROOT, "config", "sp500.csv")).read()
                            .replace("ticker,name,sector,industry", "Symbol,Security,GICS Sector,GICS Sub-Industry"))
        return FakeResp(404)


def fake_prices(symbols):
    idx = pd.date_range(end=pd.Timestamp("2026-09-30"), periods=300, freq="B")
    close = pd.DataFrame({s: [100 + i * 0.05 for i in range(300)] for s in symbols}, index=idx)
    vol = pd.DataFrame({s: [1000] * 299 + [2500] for s in symbols}, index=idx)
    return pd.concat({"Close": close, "Volume": vol}, axis=1)


def fake_enrich(tk, n_news=5):
    return {"fetched_at": util.now_utc().isoformat(timespec="seconds"), "name": tk + " Inc",
            "sector": "Technology", "market_cap": 1e9, "target_mean": 300.0, "recommendation": "buy",
            "analyst_breakdown": {"strongBuy": 5, "buy": 10, "hold": 3, "sell": 1, "strongSell": 0},
            "news": [{"title": "Noticia", "link": "https://x", "publisher": "P", "date": "2026-09-30"}],
            "insiders": {"buys": 1, "sells": 0, "buy_usd": 100000, "sell_usd": 0, "recent": []}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPSCAN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("FINNHUB_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr(futures, "fetch_prices", fake_prices)
    monkeypatch.setattr(enrich, "fetch_one", fake_enrich)
    monkeypatch.setattr(enrich.time, "sleep", lambda s: None)
    from opscan import technicals
    monkeypatch.setattr(technicals, "fetch_yahoo", lambda tks, chunk=150: {
        t: {"close": [100 + i * 0.2 for i in range(120)], "volume": [1e6] * 120} for t in tks})
    return tmp_path


def _use(monkeypatch, sess):
    monkeypatch.setattr(util, "session", lambda: sess)


TICKERS = ["RCKT", "NVDA", "SPY", "SPX", "AAPL", "XOM", "LMT", "DYN"]


def test_two_sessions_confirmation(env, monkeypatch):
    d1, d2 = dt.date(2026, 9, 29), dt.date(2026, 9, 30)
    _use(monkeypatch, FakeSession(d1))
    s1, st1 = pipeline.run("full", TICKERS)
    data = env / "data"
    latest = json.load(open(data / "latest.json"))
    assert latest["session_date"] == "2026-09-29"
    recs = {r["ticker"]: r for r in latest["records"]}
    assert set(TICKERS) <= set(recs)          # + valores de la cartera (paper.seed)
    assert recs["NVDA"]["unusual_count"] >= 1 and recs["NVDA"]["direction"] == "ALCISTA"
    assert recs["XOM"]["direction"] == "BAJISTA"
    assert recs["RCKT"]["next_catalyst"]["type"] == "PDUFA"
    assert recs["RCKT"]["checklist"]["flujo_confirmado"] is False    # aun pendiente
    assert recs["SPX"]["n_expirations"] == 8
    assert recs["LMT"]["congress"]["n_buyers"] == 2 and recs["LMT"]["congress"]["cluster"]
    flags = pd.read_csv(data / "state" / "flags.csv")
    assert set(flags["ticker"]) >= {"NVDA", "RCKT", "XOM"} and (flags["status"] == "PENDIENTE").all()

    # sesion 2: OI sube -> confirmacion de apertura -> checklist
    _use(monkeypatch, FakeSession(d2, oi_bump=True))
    s2, st2 = pipeline.run("premarket", TICKERS)
    latest = json.load(open(data / "latest.json"))
    recs = {r["ticker"]: r for r in latest["records"]}
    assert recs["RCKT"]["oi_flags"]["confirmed"] == 1
    assert recs["RCKT"]["checklist"]["flujo_confirmado"] is True
    assert recs["RCKT"]["direction"] == "ALCISTA"
    assert recs["RCKT"]["checklist"]["congreso"] and recs["RCKT"]["checklist"]["entrada"]   # 1 + 2 = 3 puntos
    assert recs["RCKT"]["components"]["flujo"] > 0          # el flujo de ayer sigue contando
    assert recs["RCKT"]["idea"]["dte"] >= 27                # vence despues del PDUFA
    idea = recs["RCKT"]["idea"]
    assert idea["legs"] and idea["legs"][0]["action"] == "COMPRAR" and idea["cost"] > 0
    assert idea["structure"].startswith("Call comprada") and len(idea["legs"]) == 1
    assert idea["choices"] and all(c["cost"] > 0 and c["kind"] == "C" for c in idea["choices"])
    paper_out = json.load(open(data / "paper.json"))
    auto = [t for t in paper_out["trades"] if t["source"] == "AUTO" and t["ticker"] == "RCKT"]
    assert auto and auto[0]["status"] == "OPEN"            # la ENTRADA abre operacion simulada
    latest_full = json.load(open(data / "latest.json"))
    assert "SPX" in latest_full["market_gex"] and latest_full["market_gex"]["SPX"]["regime"] in ("positiva", "negativa")
    assert latest_full["sectors"] and all("bias" in x for x in latest_full["sectors"])
    trk = json.load(open(data / "tracking.json"))
    assert trk["total"] >= 1 and any(st["group"] == "ENTRADA" for st in trk["stats"])
    assert recs["XOM"]["oi_flags"]["confirmed"] == 1 and recs["XOM"]["direction"] == "BAJISTA"
    assert recs["NVDA"]["components"]["oi_confirmado"] > 0
    flow = json.load(open(data / "flow.json"))
    assert len(flow["confirmed"]) >= 3
    fut = json.load(open(data / "futures.json"))
    assert len(fut["markets"]) == 20 and fut["regime"]["label"] in ("ALCISTA", "BAJISTA", "NEUTRAL")
    assert all(m["cot"] for m in fut["markets"])
    st = json.load(open(data / "status.json"))
    assert st["sources"]["Cboe opciones"]["ok"]
    assert st["sources"]["pdufa.bio (PDUFA/lecturas/AdCom)"]["count"] == 1
    assert not st["sources"]["Finnhub (resultados/IPO)"]["ok"]       # sin token: aviso, no rompe
    assert len(st["runs"]) == 2
    pol = json.load(open(data / "political.json"))
    assert pol["count"] > 100 and any(c["ticker"] == "LMT" for c in pol["clusters"])
    # enriquecimiento aplicado
    assert recs["NVDA"]["fund"]["target_mean"] == 300.0 and recs["NVDA"]["news"]

    # intradia: solo opciones, reutiliza el resto
    _use(monkeypatch, FakeSession(d2))
    pipeline.run("intraday", TICKERS)
    latest = json.load(open(data / "latest.json"))
    assert latest["mode"] == "intraday" and len(latest["records"]) >= len(TICKERS)

    # web
    site = env / "_site"
    pipeline.build_site(str(site))
    assert (site / "index.html").exists() and (site / "data" / "latest.json").exists()
    assert (site / "data" / "futures.json").exists()


def test_cboe_failure_falls_back_and_reports(env, monkeypatch):
    sess = FakeSession(dt.date(2026, 9, 30), cboe_fail={"AAPL"})
    _use(monkeypatch, sess)
    from opscan import options

    def boom(*a, **k):
        raise RuntimeError("yahoo caido")
    monkeypatch.setattr(options, "fetch_yfinance", boom)
    pipeline.run("full", ["AAPL", "NVDA"])
    latest = json.load(open(env / "data" / "latest.json"))
    recs = {r["ticker"]: r for r in latest["records"]}
    assert recs["AAPL"]["signal"] == "ERR" and "Yahoo" in recs["AAPL"]["error"]
    assert recs["NVDA"]["signal"] != "ERR"
    st = json.load(open(env / "data" / "status.json"))
    assert st["tickers_failed"] == 1


def test_full_universe_offline_performance(env, monkeypatch):
    """Universo completo (~620 valores) con red simulada: debe terminar y no romperse."""
    import time
    _use(monkeypatch, FakeSession(dt.date(2026, 9, 30)))
    t0 = time.time()
    summary, status = pipeline.run("intraday", None, offline_universe=True)
    assert summary["con_datos"] > 530
    assert time.time() - t0 < 240
