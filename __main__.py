"""CLI:  python -m opscan --mode full|premarket|intraday|smoke [--tickers AAPL,NVDA] [--build-site _site]"""
import argparse
import sys

from .pipeline import build_site, run


def main(argv=None):
    p = argparse.ArgumentParser(prog="opscan", description="Radar de flujo de opciones + catalizadores + congreso + futuros")
    p.add_argument("--mode", default="full", choices=["full", "premarket", "intraday", "smoke"])
    p.add_argument("--tickers", default="", help="lista separada por comas (sustituye al universo)")
    p.add_argument("--offline-universe", action="store_true", help="usar las listas locales de S&P/NDX")
    p.add_argument("--build-site", default="", help="carpeta donde ensamblar la web (no escanea si se usa con --no-scan)")
    p.add_argument("--no-scan", action="store_true")
    a = p.parse_args(argv)
    if not a.no_scan:
        tickers = [t.strip().upper() for t in a.tickers.split(",") if t.strip()] or None
        summary, status = run(a.mode, tickers, a.offline_universe)
        if summary["con_datos"] == 0:
            print("ERROR: ningun valor con datos de opciones", file=sys.stderr)
            if a.build_site:
                build_site(a.build_site)
            return 2
    if a.build_site:
        build_site(a.build_site)
    return 0


if __name__ == "__main__":
    sys.exit(main())
