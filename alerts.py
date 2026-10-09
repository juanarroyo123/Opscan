"""Alertas opcionales por Telegram (secrets TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID)."""
import os

from .util import log, session


def format_alert(r, site_url=""):
    ck = r["checklist"]
    lines = [f"OpScan · {r['ticker']} {r['direction']} - score {r['score']} ({r['signal']})",
             f"Checklist: {ck['puntos']} pts | congreso {'si' if ck['congreso'] else 'no'} | "
             f"futuros {'si' if ck['futuros'] else 'no'} | flujo confirmado {'si' if ck['flujo_confirmado'] else 'no'}"]
    for x in r["reasons"][:5]:
        lines.append("- " + x)
    if r.get("idea"):
        i = r["idea"]
        lines.append(f"Idea (educativa): {i['structure']} {i.get('expiration') or ''} - {i['strikes']}")
    if site_url:
        lines.append(site_url)
    return "\n".join(lines)


def send_telegram(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    r = session().post(f"https://api.telegram.org/bot{token}/sendMessage",
                       json={"chat_id": chat, "text": text, "disable_web_page_preview": True}, timeout=20)
    return r.status_code == 200


def only_trades(cfg):
    """Modo 'solo operaciones': Telegram solo avisa de compras/ventas del Robot y de cuando cerrar."""
    return str((cfg.get("alerts") or {}).get("mode", "solo_operaciones")) == "solo_operaciones"


def _fd(d):
    try:
        y, m, dd = str(d)[:10].split("-")
        return f"{dd}/{m}/{y}"
    except ValueError:
        return str(d or "")


def _px(x):
    return f"${x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _leg_txt(t):
    l = (t.get("legs") or [{}])[0]
    kind = "CALL" if str(l.get("kind", "C")).upper().startswith("C") else "PUT"
    return f"{kind} {t['ticker']} {l.get('strike', 0):g} · vence {_fd(l.get('expiration'))}", l


def msg_buy(t):
    txt, l = _leg_txt(t)
    n = int(t.get("contracts") or 1)
    return (f"🟢 {_fd(t.get('opened'))} · Robot COMPRA\n{txt}\n"
            f"{n} contrato{'s' if n != 1 else ''} · prima {_px(l.get('entry') or 0)}")


def msg_sell(t):
    txt, l = _leg_txt(t)
    n = int(t.get("contracts") or 1)
    px = (t.get("exit_value") or 0) / (100 * n) if n else 0
    return (f"🔴 {_fd(t.get('closed'))} · Robot VENDE\n{txt}\n"
            f"{n} contrato{'s' if n != 1 else ''} · prima {_px(px)}")


def msg_close_advice(t, session_date):
    txt, _ = _leg_txt(t)
    why = ((t.get("advice") or {}).get("reasons") or [""])[0]
    acc = {"MANUAL": "OpScan", "RSI": "Soportes + RSI"}.get(t.get("source"), t.get("source"))
    return f"⚠️ {_fd(session_date)} · Toca CERRAR ({acc})\n{txt}\n{why}".rstrip()


def process(records, cfg, sent, session_date, status):
    acfg = cfg["alerts"]
    if not acfg.get("telegram") or not os.environ.get("TELEGRAM_BOT_TOKEN") or only_trades(cfg):
        return sent
    site = os.environ.get("OPSCAN_SITE_URL", "")
    n = 0
    for r in records:
        if r.get("score", 0) < acfg["min_score"]:
            continue
        if acfg.get("only_entries") and not r.get("checklist", {}).get("entrada"):
            continue
        key = f"{session_date}:{r['ticker']}:{r['direction']}"
        if key in sent:
            continue
        try:
            if send_telegram(format_alert(r, site)):
                sent[key] = True
                n += 1
        except Exception as e:
            log(f"telegram: {e}")
    # limpiar claves antiguas
    keep = sorted(sent.keys())[-500:]
    status.ok("Alertas Telegram", n)
    return {k: True for k in keep}


def daily_summary(records, summary, session, sent, paper_summary=None, confirmed_today=None, gex=None):
    """Resumen diario (una vez por sesion, en la ejecucion de cierre)."""
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        return sent
    key = f"{session}:resumen"
    if key in sent:
        return sent
    site = os.environ.get("OPSCAN_SITE_URL", "")
    ok = [r for r in records if r.get("signal") not in (None, "ERR")]
    lines = [f"OpScan · resumen {session}",
             f"Entradas: {summary.get('entradas', 0)} | ALTA: {summary.get('alta', 0)} | MEDIA: {summary.get('media', 0)} | "
             f"pre-alertas: {summary.get('pre_alertas', 0)}"]
    if gex:
        lines.append(f"Gamma SPX {gex.get('regime')} (flip {gex.get('flip')}) - "
                     + ("mercado tranquilo" if gex.get("regime") == "positiva" else "movimientos amplificados"))
    lines.append("")
    lines.append("Top 5:")
    for r in ok[:5]:
        ck = r.get("checklist") or {}
        tag = " ENTRADA" if ck.get("entrada") else (" pre" if ck.get("pre_alerta") else "")
        lines.append(f"- {r['ticker']} {r['direction']} {r['score']}{tag}")
    if confirmed_today:
        lines.append("")
        lines.append(f"Confirmadas por OI hoy: {len(confirmed_today)}")
        for c in confirmed_today[:5]:
            lines.append(f"- {c['ticker']} {c['kind']} {c['strike']} {c['expiration']} ({c['direction']})")
    if paper_summary:
        o, cl = paper_summary.get("open", {}), paper_summary.get("closed", {})
        lines.append("")
        lines.append(f"Cartera simulada: {o.get('n', 0)} abiertas (P&L ${o.get('pnl', 0)}), "
                     f"{cl.get('n', 0)} cerradas (P&L ${cl.get('pnl', 0)}, aciertos {cl.get('win_rate') or '-'}%)")
    if site:
        lines.append(site)
    try:
        if send_telegram("\n".join(lines)):
            sent[key] = True
    except Exception as e:
        log(f"telegram resumen: {e}")
    return sent
