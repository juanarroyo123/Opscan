"""Utilidades comunes: HTTP con reintentos, fechas de mercado, numeros, JSON."""
import datetime as dt
import json
import math
import os
import random
import re
import tempfile
import threading
import time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def sec_user_agent():
    # La SEC exige un User-Agent con contacto real: define SEC_USER_AGENT="Nombre email@dominio"
    return os.environ.get("SEC_USER_AGENT", "OpScan research opscan-bot@users.noreply.github.com")


_local = threading.local()


def session():
    """requests.Session por hilo (requests no es thread-safe compartiendo sesion)."""
    s = getattr(_local, "session", None)
    if s is None:
        import requests
        s = requests.Session()
        s.headers.update({"User-Agent": DEFAULT_UA, "Accept": "application/json,text/html,*/*"})
        _local.session = s
    return s


class HttpError(Exception):
    def __init__(self, status, url, msg=""):
        super().__init__(f"HTTP {status} {url} {msg}".strip())
        self.status = status


def get(url, params=None, headers=None, timeout=30, retries=3, backoff=1.5):
    """GET con reintentos en 429/5xx y errores de red. Lanza HttpError si falla."""
    last = None
    for attempt in range(retries + 1):
        try:
            r = session().get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                wait = backoff * (2 ** attempt) + random.random()
                ra = r.headers.get("Retry-After")
                if ra and ra.isdigit():
                    wait = max(wait, min(int(ra), 60))
                time.sleep(wait)
                continue
            raise HttpError(r.status_code, url)
        except HttpError:
            raise
        except Exception as e:  # red, timeout...
            last = e
            if attempt < retries:
                time.sleep(backoff * (2 ** attempt) + random.random())
                continue
    raise HttpError("NET", url, str(last))


def get_json(url, **kw):
    return get(url, **kw).json()


# ---------------------------------------------------------------- fechas
def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def now_et():
    return dt.datetime.now(ET)


def today_et():
    return now_et().date()


def iso_now():
    return now_utc().isoformat(timespec="seconds")


def parse_date(s):
    """Devuelve date o None a partir de formatos habituales."""
    if s is None:
        return None
    if isinstance(s, dt.datetime):
        return s.date()
    if isinstance(s, dt.date):
        return s
    s = str(s).strip()
    if not s or s.lower() in ("nan", "none", "nat"):
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    for fmt in ("%Y/%m/%d", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y", "%d/%m/%Y", "%B %Y", "%b %Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    m = re.match(r"^(\d{4})-(\d{2})$", s)
    if m:
        return dt.date(int(m.group(1)), int(m.group(2)), 1)
    return None


def iso(d):
    d = parse_date(d)
    return d.isoformat() if d else None


def days_between(a, b):
    a, b = parse_date(a), parse_date(b)
    if not a or not b:
        return None
    return (b - a).days


def third_friday(year, month):
    d = dt.date(year, month, 1)
    offset = (4 - d.weekday()) % 7
    return d + dt.timedelta(days=offset + 14)


# ---------------------------------------------------------------- numeros
def num(x, default=0.0):
    try:
        if x is None:
            return default
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


def num_or_none(x):
    v = num(x, None)
    return v


def rnd(x, n=2):
    if x is None:
        return None
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return None
        return round(v, n)
    except (TypeError, ValueError):
        return None


def safe_ratio(a, b, n=3):
    a, b = num(a), num(b)
    if b <= 0:
        return None
    return round(a / b, n)


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


# ---------------------------------------------------------------- JSON
def _default(o):
    if isinstance(o, (dt.date, dt.datetime)):
        return o.isoformat()
    if hasattr(o, "item"):
        return o.item()
    if isinstance(o, set):
        return sorted(o)
    return str(o)


def clean_nans(o):
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else o
    if isinstance(o, dict):
        return {k: clean_nans(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean_nans(v) for v in o]
    return o


def write_json(path, obj, indent=None):
    """Escritura atomica (nunca deja un JSON a medias que rompa la web)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(clean_nans(obj), f, ensure_ascii=False, indent=indent,
                  default=_default, separators=None if indent else (",", ":"))
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


# ---------------------------------------------------------------- estado de fuentes
class Status:
    """Registro de salud por fuente de datos, se publica en status.json."""

    def __init__(self):
        self.sources = {}
        self._lock = threading.Lock()

    def ok(self, name, count=None, note=""):
        with self._lock:
            self.sources[name] = {"ok": True, "count": count, "note": note, "at": iso_now()}

    def fail(self, name, err, count=None):
        with self._lock:
            self.sources[name] = {"ok": False, "count": count, "note": str(err)[:300], "at": iso_now()}

    def to_dict(self):
        return dict(self.sources)


def log(msg):
    print(f"[{now_utc().strftime('%H:%M:%S')}] {msg}", flush=True)
