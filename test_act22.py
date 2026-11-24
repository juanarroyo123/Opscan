"""Act. 22: el escaneo no debe caerse si Cboe falla o el VIX llega sin variacion diaria."""
from opscan import market, options
from opscan.util import HttpError


def test_semaforo_vix_sin_variacion():
    recs = [{"ticker": "VIX", "price": 27.3, "change_pct": None},
            {"ticker": "SPY", "price": 560.0, "change_pct": -1.2}]
    sem = market.semaforo(recs, None, None)
    assert sem["level"] == "rojo"
    assert any("VIX 27.3" in w for w in sem["why"])


def test_fetch_cboe_registra_errores(monkeypatch):
    options.CBOE_ERRORS.clear()
    def boom(url, **kw):
        assert kw["headers"]["Origin"] == "https://www.cboe.com"
        raise HttpError(403, url)
    monkeypatch.setattr(options, "get_json", boom)
    try:
        options.fetch_cboe("AAPL")
        assert False
    except HttpError:
        pass
    assert options.CBOE_ERRORS.get("403") == 2
