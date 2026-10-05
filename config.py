"""Carga de configuracion (config.yml) con valores por defecto y rutas."""
import copy
import os

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.yml")
CONFIG_DIR = os.path.join(ROOT, "config")
DOCS_DIR = os.path.join(ROOT, "docs")


def data_dir():
    """Carpeta de salida. En GitHub Actions es el checkout de la rama `data`."""
    return os.environ.get("OPSCAN_DATA_DIR", os.path.join(ROOT, "data"))


def state_dir():
    return os.path.join(data_dir(), "state")


DEFAULTS = {
    "universe": {
        "sp500": True, "nasdaq100": True, "etfs": ["SPY", "QQQ", "IWM"],
        "indices": ["SPX", "NDX", "VIX"], "watchlist": [], "exclude": [],
        "add_catalyst_tickers": True, "max_tickers": 1300,
        "midcap400": True, "russell1000": False, "min_avg_opt_vol": 300,
    },
    "options": {
        "sold_weight": 0.0,
        "money_weights": {"otm": 2.0, "atm": 1.3, "lottery": 1.2, "itm": 0.4},
        "workers": 12, "max_dte": 800, "min_volume": 100, "min_vol_oi": 1.0,
        "min_premium": 50000, "big_premium": 1000000, "min_dte_unusual": 2, "min_premium_share": 0.003, "max_abs_delta_hedge": 0.90,
        "top_flow_rows": 800, "yfinance_fallback_max": 40,
        "oi_confirm_ratio": 0.5, "oi_confirm_window": 5,
    },
    "scoring": {
        "catalyst_horizon_days": 45, "congress_window_days": 90,
        "congress_cluster_days": 30, "entry_min_points": 3, "alta": 55, "media": 30,
        "idea_style": "simple", "entry_min_score": 45, "entry_min_confirmed_premium": 200000,
        "entry_min_confirm_rate": 0.4,
    },
    "enrich": {"top_n": 75, "news": 5},
    "insiders": {"enabled": True, "days": 25, "max_fetch": 1200},
    "paper": {"capital": 10000, "max_pct_trade": 5, "max_new_per_day": 3, "max_open_auto": 8,
              "skip_bad_liquidity": True},
    "calendar": {
        "horizon_days": 90, "trial_phases": ["PHASE2", "PHASE3"],
        "sec_terms": ["merger agreement", "tender offer", "acquisition agreement"],
        "include_clinicaltrials": True, "include_sec": True, "include_opex": True,
    },
    "congress": {"horizon_days": 365, "include_executive": True, "max_rows": 2500, "max_filer_trades": 60},
    "futures": {"cot_years": 3, "markets": [], "sector_links": {}},
    "alerts": {"telegram": True, "min_score": 60, "only_entries": True},
}


def _merge(base, user):
    out = copy.deepcopy(base)
    for k, v in (user or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        elif v is not None:
            out[k] = v
    return out


def load_config(path=None):
    path = path or CONFIG_PATH
    user = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
    cfg = _merge(DEFAULTS, user)
    u = cfg["universe"]
    for k in ("etfs", "indices", "watchlist", "exclude"):
        u[k] = [str(t).strip().upper() for t in (u.get(k) or []) if str(t).strip()]
    return cfg
