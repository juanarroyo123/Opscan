"""Ventas en corto: volumen diario en corto de FINRA (gratis, todos los valores de EEUU)
+ % del capital flotante en corto (Yahoo, para los valores enriquecidos).

Riesgo de apreton de cortos (short squeeze) = mucho corto + flujo alcista en opciones
+ precio subiendo/volumen alto. Si los cortos tienen que recomprar, la subida se acelera.
"""
import datetime as dt

from .util import get, log

FINRA_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{d}.txt"


def parse_finra(text):
    """'Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market' -> {sym: (short, total)}"""
    out = {}
    for line in (text or "").splitlines()[1:]:
        p = line.strip().split("|")
        if len(p) < 5 or not p[1]:
            continue
        try:
            sv, tv = float(p[2]), float(p[4])
        except ValueError:
            continue
        sym = p[1].strip().upper().replace("/", ".")
        a = out.get(sym, (0.0, 0.0))
        out[sym] = (a[0] + sv, a[1] + tv)
    return out


def fetch_short_volume(days=5, max_back=10, today=None):
    """Suma los ultimos `days` ficheros diarios disponibles. -> {sym: {ratio_5d, ratio_last, days}}"""
    today = today or dt.date.today()
    files = []
    d = today
    for _ in range(max_back + days):
        d -= dt.timedelta(days=1)
        if d.weekday() >= 5:
            continue
        try:
            r = get(FINRA_URL.format(d=d.strftime("%Y%m%d")), timeout=30, retries=1)
            files.append(parse_finra(r.text))
        except Exception:
            continue
        if len(files) >= days:
            break
    if not files:
        raise RuntimeError("sin ficheros FINRA")
    out = {}
    syms = set().union(*[f.keys() for f in files])
    for s in syms:
        sv = sum(f.get(s, (0, 0))[0] for f in files)
        tv = sum(f.get(s, (0, 0))[1] for f in files)
        last = files[0].get(s)
        if tv > 0:
            out[s] = {"ratio_5d": round(sv / tv, 3),
                      "ratio_last": round(last[0] / last[1], 3) if last and last[1] else None,
                      "days": sum(1 for f in files if s in f)}
    log(f"FINRA: {len(files)} dias, {len(out)} simbolos")
    return out


def squeeze_risk(short_pct_float, short_vol_ratio, direction, change_pct, rel_vol, cp_ratio):
    """Devuelve {level, reasons} o None. Niveles: ALTO / MEDIO."""
    pts, why = 0, []
    if short_pct_float is not None:
        if short_pct_float >= 0.20:
            pts += 2; why.append(f"{short_pct_float*100:.0f}% del flotante en corto")
        elif short_pct_float >= 0.12:
            pts += 1; why.append(f"{short_pct_float*100:.0f}% del flotante en corto")
    if short_vol_ratio is not None and short_vol_ratio >= 0.55:
        pts += 1; why.append(f"{short_vol_ratio*100:.0f}% del volumen diario es venta en corto")
    if pts == 0:
        return None
    if direction == "ALCISTA":
        pts += 1; why.append("flujo de opciones alcista")
    if cp_ratio is not None and cp_ratio >= 2:
        pts += 1; why.append(f"calls/puts {cp_ratio:.1f}x")
    if change_pct is not None and change_pct > 2:
        pts += 1; why.append(f"precio +{change_pct:.1f}% hoy")
    if rel_vol is not None and rel_vol >= 1.5:
        pts += 1; why.append(f"volumen de opciones {rel_vol}x")
    if pts >= 5:
        return {"level": "ALTO", "points": pts, "reasons": why}
    if pts >= 3:
        return {"level": "MEDIO", "points": pts, "reasons": why}
    return None
