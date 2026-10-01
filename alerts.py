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


def process(records, cfg, sent, session_date, status):
    acfg = cfg["alerts"]
    if not acfg.get("telegram") or not os.environ.get("TELEGRAM_BOT_TOKEN"):
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
