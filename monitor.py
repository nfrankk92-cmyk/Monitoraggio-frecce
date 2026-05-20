"""
Monitor disponibilita' treni Brescia -> Milano Centrale (07:34 / 08:39).
Esegue UNA singola verifica e invia notifica Telegram se trova posti Standard.
Pensato per girare come step di GitHub Actions (cron schedule).

Configurazione tramite variabili d'ambiente:
  TELEGRAM_TOKEN    - token del bot Telegram
  TELEGRAM_CHAT_ID  - chat_id destinatario
  TARGET_DATE       - data nel formato YYYY-MM-DD

In locale (fallback) legge da config.json se le env vars non sono settate.
"""
import json
import os
import re
import sys
import time
import requests
from datetime import datetime

DEPARTURE_ID  = 830001717   # Brescia
ARRIVAL_ID    = 830001700   # Milano Centrale
TARGET_TIMES  = {"07:34", "08:39"}

BASE          = "https://www.lefrecce.it/Channels.Website.BFF.WEB"
CSRF_URL      = f"{BASE}/website/whitelist/enabled"
SOLUTIONS_URL = f"{BASE}/website/ticket/solutions"


def load_config():
    env_keys = ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "TARGET_DATE")
    if all(os.environ.get(k) for k in env_keys):
        return {
            "telegram_token":   os.environ["TELEGRAM_TOKEN"],
            "telegram_chat_id": os.environ["TELEGRAM_CHAT_ID"],
            "target_date":      os.environ["TARGET_DATE"],
        }
    local = os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.exists(local):
        with open(local, encoding="utf-8") as f:
            return json.load(f)
    raise SystemExit("Configurazione mancante: setta env vars o config.json")


def send_telegram(token, chat_id, message):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(
        url,
        json={"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=15,
    )
    r.raise_for_status()


def build_headers(csrf=None):
    h = {
        "User-Agent":         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        "Accept":             "application/json, application/pdf, text/calendar",
        "Accept-Language":    "it-IT,it;q=0.9",
        "channel":            "41",
        "content-type":       "application/json",
        "origin":             "https://www.lefrecce.it",
        "referer":            "https://www.lefrecce.it/Channels.Website.WEB/",
        "x-requested-with":   "Fetch",
        "whitelabel_referrer": "www.lefrecce.it",
        "callertimestamp":    str(int(time.time() * 1000)),
    }
    if csrf:
        h["x-csrf-token"] = csrf
    return h


def get_csrf_token(session):
    resp = session.post(CSRF_URL, json={}, headers=build_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json().get("token")


def check_trains():
    config      = load_config()
    target_date = config["target_date"]
    tg_token    = config["telegram_token"]
    chat_id     = config["telegram_chat_id"]

    session = requests.Session()
    csrf    = get_csrf_token(session)

    body = {
        "departureLocationId": DEPARTURE_ID,
        "arrivalLocationId":   ARRIVAL_ID,
        "departureTime":       f"{target_date}T07:00:00.000",
        "adults":   1,
        "children": 0,
        "criteria": {
            "frecceOnly":    True,
            "regionalOnly":  False,
            "intercityOnly": False,
            "tourismOnly":   False,
            "noChanges":     False,
            "order":         "DEPARTURE_DATE",
            "offset":        0,
            "limit":         10,
        },
        "advancedSearchRequest": {
            "bestFare":             False,
            "bikeFilter":           False,
            "forwardDiscountCodes": [],
        },
    }

    resp = session.post(SOLUTIONS_URL, json=body, headers=build_headers(csrf), timeout=20)
    resp.raise_for_status()
    data = resp.json()

    found = []
    for item in data.get("solutions", []):
        sol = item.get("solution", {})
        dep = sol.get("departureTime", "")
        m   = re.search(r"T(\d{2}:\d{2})", dep)
        if not m:
            continue
        dep_time = m.group(1)
        if dep_time not in TARGET_TIMES:
            continue

        trains = sol.get("trains", [])
        cat    = trains[0].get("trainCategory", "") if trains else ""
        num    = trains[0].get("name", "")           if trains else ""

        standard_offer = None
        for grid in item.get("grids", []):
            for service in grid.get("services", []):
                if service.get("name", "").upper() == "STANDARD":
                    for offer in service.get("offers", []):
                        if offer.get("status") == "SALEABLE" and offer.get("availableAmount", 0) > 0:
                            standard_offer = offer
                            break
                if standard_offer:
                    break
            if standard_offer:
                break

        if not standard_offer:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {cat} {num} {dep_time} - Standard non disponibile")
            continue

        price_obj = standard_offer.get("price") or {}
        price_str = f"{price_obj.get('amount', '?')}EUR" if price_obj else "?"
        found.append(f"{cat} {num} - partenza {dep_time} - Standard {price_str}")

    if found:
        lines = "\n".join(found)
        msg   = (
            f"<b>Posto Standard disponibile! Brescia → Milano</b>\n"
            f"Data: {target_date}\n\n"
            f"{lines}\n\n"
            f'<a href="https://www.lefrecce.it">Acquista su Le Frecce</a>'
        )
        send_telegram(tg_token, chat_id, msg)
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] NOTIFICA INVIATA: {found}")
    else:
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Nessun posto Standard per {target_date}")


if __name__ == "__main__":
    try:
        check_trains()
    except Exception as e:
        print(f"[ERRORE] {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
