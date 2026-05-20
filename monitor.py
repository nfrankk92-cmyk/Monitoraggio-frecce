"""
Monitor interattivo treni Brescia -> Milano Centrale (07:34 / 08:39, classe Standard).

Pensato come step di GitHub Actions con cron */5 min:
  1. Legge messaggi Telegram nuovi (getUpdates con offset)
  2. Processa eventuali comandi: /aggiungi /rimuovi /lista /stop /check /help
  3. Per ogni data attiva, controlla l'API Le Frecce
  4. Notifica via Telegram solo sulle transizioni "non disponibile" -> "disponibile"
  5. Salva stato in state.json (committato dal workflow)

Variabili d'ambiente richieste:
  TELEGRAM_TOKEN
  TELEGRAM_CHAT_ID  (i comandi ricevuti da altri chat sono ignorati)
"""
import json
import os
import re
import sys
import time
from datetime import datetime, date

import requests

DEPARTURE_ID  = 830001717
ARRIVAL_ID    = 830001700
TARGET_TIMES  = ["07:34", "08:39"]

BASE          = "https://www.lefrecce.it/Channels.Website.BFF.WEB"
CSRF_URL      = f"{BASE}/website/whitelist/enabled"
SOLUTIONS_URL = f"{BASE}/website/ticket/solutions"

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

HELP_TEXT = (
    "<b>Monitor Treni Brescia → Milano</b>\n"
    "Controlla ogni ~5 min i treni delle <b>07:34</b> e <b>08:39</b> (classe Standard).\n\n"
    "<b>Comandi:</b>\n"
    "<code>/aggiungi 2026-05-21</code> — aggiunge una data da monitorare\n"
    "<code>/rimuovi 2026-05-21</code> — rimuove una data\n"
    "<code>/lista</code> — mostra le date attive\n"
    "<code>/stop</code> — rimuove tutte le date\n"
    "<code>/check</code> — stato attuale dei treni adesso\n"
    "<code>/help</code> — questa guida"
)


# ---------- stato ----------

def _default_state():
    return {"active_dates": [], "last_update_id": 0, "last_status": {}}


def load_state():
    if not os.path.exists(STATE_FILE):
        return _default_state()
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
        s.setdefault("active_dates", [])
        s.setdefault("last_update_id", 0)
        s.setdefault("last_status", {})
        return s
    except Exception as e:
        print(f"[WARN] state.json corrotto, reinizializzo: {e}")
        return _default_state()


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")


# ---------- Telegram ----------

def tg_send(token, chat_id, text):
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": True,
        },
        timeout=15,
    )
    r.raise_for_status()


def tg_get_updates(token, offset):
    r = requests.get(
        f"https://api.telegram.org/bot{token}/getUpdates",
        params={"offset": offset, "timeout": 0},
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("result", [])


# ---------- comandi ----------

def parse_date(s):
    try:
        d = datetime.strptime(s.strip(), "%Y-%m-%d").date()
        return d, None
    except ValueError:
        return None, "Formato data non valido. Usa <code>YYYY-MM-DD</code> (es: <code>2026-05-21</code>)"


def fmt_date_it(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date().strftime("%d/%m/%Y")
    except ValueError:
        return s


def handle_command(text, state):
    """Restituisce (reply: str|None, force_check: bool)."""
    parts = text.strip().split(maxsplit=1)
    cmd   = parts[0].lower() if parts else ""
    arg   = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/start", "/help"):
        return HELP_TEXT, False

    if cmd == "/lista":
        if not state["active_dates"]:
            return "Nessuna data attiva.\nUsa <code>/aggiungi 2026-05-21</code>", False
        lines = "\n".join(f"  • {fmt_date_it(d)}" for d in sorted(state["active_dates"]))
        return f"<b>Date attive:</b>\n{lines}", False

    if cmd == "/stop":
        n = len(state["active_dates"])
        state["active_dates"] = []
        state["last_status"]  = {}
        return f"Monitoraggio disattivato ({n} date rimosse).", False

    if cmd == "/aggiungi":
        if not arg:
            return "Uso: <code>/aggiungi YYYY-MM-DD</code>", False
        d, err = parse_date(arg)
        if err:
            return err, False
        if d < date.today():
            return f"La data {d.strftime('%d/%m/%Y')} è nel passato.", False
        ds = d.isoformat()
        if ds in state["active_dates"]:
            return f"{fmt_date_it(ds)} è già tra le date monitorate.", False
        state["active_dates"].append(ds)
        return (
            f"✅ Aggiunto <b>{fmt_date_it(ds)}</b>.\n"
            f"Date attive: {len(state['active_dates'])}\n"
            f"(primo check entro 5 min)"
        ), False

    if cmd == "/rimuovi":
        if not arg:
            return "Uso: <code>/rimuovi YYYY-MM-DD</code>", False
        d, err = parse_date(arg)
        if err:
            return err, False
        ds = d.isoformat()
        if ds not in state["active_dates"]:
            return f"{fmt_date_it(ds)} non è tra le date monitorate.", False
        state["active_dates"].remove(ds)
        state["last_status"] = {
            k: v for k, v in state["last_status"].items()
            if not k.startswith(ds + "|")
        }
        return f"❌ Rimosso <b>{fmt_date_it(ds)}</b>.", False

    if cmd == "/check":
        return None, True   # gestito fuori

    return None, False  # comando sconosciuto: silenzio


# ---------- API Le Frecce ----------

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
    r = session.post(CSRF_URL, json={}, headers=build_headers(), timeout=15)
    r.raise_for_status()
    return r.json().get("token")


def check_date(session, csrf, target_date):
    """Restituisce dict {hh:mm -> (saleable, label, price_or_None)}"""
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
    r = session.post(SOLUTIONS_URL, json=body, headers=build_headers(csrf), timeout=20)
    r.raise_for_status()
    data = r.json()

    out = {}
    for item in data.get("solutions", []):
        sol = item.get("solution", {})
        dep = sol.get("departureTime", "")
        m   = re.search(r"T(\d{2}:\d{2})", dep)
        if not m:
            continue
        t = m.group(1)
        if t not in TARGET_TIMES:
            continue

        trains = sol.get("trains", [])
        cat    = trains[0].get("trainCategory", "") if trains else ""
        num    = trains[0].get("name", "")           if trains else ""
        label  = f"{cat} {num}".strip()

        saleable, price = False, None
        for grid in item.get("grids", []):
            for svc in grid.get("services", []):
                if svc.get("name", "").upper() == "STANDARD":
                    for off in svc.get("offers", []):
                        if off.get("status") == "SALEABLE" and off.get("availableAmount", 0) > 0:
                            saleable = True
                            p = off.get("price") or {}
                            price = p.get("amount")
                            break
        out[t] = (saleable, label, price)
    return out


# ---------- main ----------

def main():
    tg_token       = os.environ["TELEGRAM_TOKEN"]
    chat_id_raw    = os.environ["TELEGRAM_CHAT_ID"]
    try:
        owner_chat = int(chat_id_raw)
    except ValueError:
        owner_chat = chat_id_raw

    state = load_state()

    # === 1. Comandi Telegram ===
    try:
        updates = tg_get_updates(tg_token, state["last_update_id"] + 1)
    except Exception as e:
        print(f"[WARN] getUpdates fallito: {e}")
        updates = []

    forced_check = False
    for upd in updates:
        state["last_update_id"] = max(state["last_update_id"], upd.get("update_id", 0))
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            continue
        chat = msg.get("chat", {})
        if chat.get("id") != owner_chat:
            print(f"[INFO] Ignoro messaggio da chat {chat.get('id')} (non autorizzato)")
            continue
        text = msg.get("text", "") or ""
        if not text.startswith("/"):
            continue

        reply, fc = handle_command(text, state)
        forced_check = forced_check or fc
        if fc:
            try:
                tg_send(tg_token, owner_chat, "🔄 Eseguo un check immediato...")
            except Exception as e:
                print(f"[WARN] reply /check fallita: {e}")
        elif reply:
            try:
                tg_send(tg_token, owner_chat, reply)
            except Exception as e:
                print(f"[WARN] sendMessage fallito: {e}")

    # === 2. Pulizia automatica: rimuovi date passate ===
    today_iso = date.today().isoformat()
    expired   = [d for d in state["active_dates"] if d < today_iso]
    if expired:
        for d in expired:
            state["active_dates"].remove(d)
            state["last_status"] = {
                k: v for k, v in state["last_status"].items()
                if not k.startswith(d + "|")
            }
        try:
            tg_send(tg_token, owner_chat,
                    "🗓 Date scadute rimosse automaticamente: " +
                    ", ".join(fmt_date_it(d) for d in expired))
        except Exception as e:
            print(f"[WARN] notifica scadenza fallita: {e}")

    # === 3. Check disponibilita' per ogni data attiva ===
    check_lines = []
    if state["active_dates"]:
        try:
            session = requests.Session()
            csrf    = get_csrf_token(session)
        except Exception as e:
            print(f"[ERROR] CSRF fallito: {e}")
            save_state(state)
            return

        for d in sorted(state["active_dates"]):
            try:
                statuses = check_date(session, csrf, d)
            except Exception as e:
                print(f"[ERROR] check_date {d}: {e}")
                continue

            for t in TARGET_TIMES:
                if t not in statuses:
                    continue
                saleable, label, price = statuses[t]
                key          = f"{d}|{t}"
                was_saleable = state["last_status"].get(key, False)
                state["last_status"][key] = saleable

                tag  = "✅" if saleable else "—"
                line = f"{tag} {fmt_date_it(d)} {label} {t}"
                if saleable and price is not None:
                    line += f" • {price}€"
                check_lines.append(line)
                print(line)

                # notifica solo sulla transizione FALSE -> TRUE
                if saleable and not was_saleable:
                    msg = (
                        f"<b>🎉 Posto Standard disponibile!</b>\n"
                        f"📅 {fmt_date_it(d)}\n"
                        f"🚄 {label} - partenza <b>{t}</b>\n"
                        f"💶 {price}€\n\n"
                        f'<a href="https://www.lefrecce.it">Acquista subito su Le Frecce</a>'
                    )
                    try:
                        tg_send(tg_token, owner_chat, msg)
                    except Exception as e:
                        print(f"[WARN] notifica disponibilita fallita: {e}")
    else:
        print("Nessuna data attiva, salto controllo treni.")

    # === 4. Risposta a /check ===
    if forced_check:
        body = "<b>Stato attuale:</b>\n" + "\n".join(check_lines) if check_lines else "Nessuna data attiva."
        try:
            tg_send(tg_token, owner_chat, body)
        except Exception as e:
            print(f"[WARN] reply /check fallita: {e}")

    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[FATAL] {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
