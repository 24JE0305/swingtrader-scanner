"""
SwingTrader — Monitor (Phase 4b)
Runs every 15 min on weekdays. Two jobs each run:
  1. Live stop-loss check, only near 9:30/11:30/14:00/15:00 IST.
  2. Deadline sweep for every pending question (buy-intent 7PM cutoff,
     buy-price ask-at-12:45 + 1hr deadline, sell-confirm reminder chain).
"""
import os, json, datetime
from zoneinfo import ZoneInfo
import pandas as pd
import yfinance as yf
import requests

IST = ZoneInfo("Asia/Kolkata")
STOP_SLOTS = [("09:30", "0930"), ("11:30", "1130"), ("14:00", "1400"), ("15:00", "1500")]
REMIND2_TIME = datetime.time(12, 45)
REMIND3_TIME = datetime.time(14, 0)
GIVEUP_TIME  = datetime.time(14, 30)
BUY_DEADLINE = datetime.time(19, 0)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
CF_ACCOUNT_ID      = os.environ["CF_ACCOUNT_ID"]
CF_KV_NAMESPACE_ID = os.environ["CF_KV_NAMESPACE_ID"]
CF_API_TOKEN       = os.environ["CF_API_TOKEN"]
KV_URL = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}"
KV_HEADERS = {"Authorization": f"Bearer {CF_API_TOKEN}"}


def kv_get(key, default=None):
    r = requests.get(f"{KV_URL}/values/{key}", headers=KV_HEADERS, timeout=15)
    if r.status_code == 404: return default
    r.raise_for_status(); return r.text

def kv_put(key, value, ttl=None):
    params = {"expiration_ttl": ttl} if ttl else {}
    r = requests.put(f"{KV_URL}/values/{key}", headers=KV_HEADERS, params=params, data=value, timeout=15)
    r.raise_for_status()

def kv_delete(key):
    r = requests.delete(f"{KV_URL}/values/{key}", headers=KV_HEADERS, timeout=15)
    if r.status_code not in (200, 404): r.raise_for_status()

def get_positions(): return json.loads(kv_get("positions", "[]"))
def save_positions(p): kv_put("positions", json.dumps(p))
def get_pending_index(): return json.loads(kv_get("pending_index", "[]"))
def save_pending_index(ids): kv_put("pending_index", json.dumps(ids))

def get_pending(mid):
    raw = kv_get(f"pending:{mid}")
    return json.loads(raw) if raw else None

def set_pending(mid, data):
    kv_put(f"pending:{mid}", json.dumps(data))

def clear_pending(mid):
    kv_delete(f"pending:{mid}")
    ids = get_pending_index()
    save_pending_index([i for i in ids if i != str(mid)])

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)
    r.raise_for_status()
    result = r.json().get("result")
    return result["message_id"] if result else None

def send_and_track(text, data):
    mid = send_telegram(text)
    if mid:
        data = {**data, "created": int(datetime.datetime.now().timestamp() * 1000)}
        set_pending(mid, data)
        ids = get_pending_index()
        ids.append(str(mid))
        save_pending_index(ids)
    return mid


def to_ist(epoch_ms):
    return datetime.datetime.fromtimestamp(epoch_ms / 1000, tz=IST)


# ── Job 1: live stop-loss check ──
def stop_loss_check(now_ist, positions):
    active_slot = None
    for target_str, label in STOP_SLOTS:
        h, m = map(int, target_str.split(":"))
        target = now_ist.replace(hour=h, minute=m, second=0, microsecond=0)
        if abs((now_ist - target).total_seconds()) <= 7 * 60:
            active_slot = label
            break
    if not active_slot:
        return positions

    date_str = now_ist.date().isoformat()
    flag_key = f"stopcheck:{date_str}:{active_slot}"
    if kv_get(flag_key):
        return positions  # already ran this slot
    kv_put(flag_key, "1", ttl=90000)

    updated = []
    for pos in positions:
        if pos.get("status") != "open" or "stop_price" not in pos:
            updated.append(pos)
            continue
        try:
            raw = yf.download(pos["symbol"] + ".NS", period="1d", interval="5m", progress=False)
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = raw.columns.get_level_values(0)
            if raw.empty:
                updated.append(pos); continue
            live_price = float(raw["Close"].iloc[-1])
        except Exception:
            updated.append(pos); continue

        if live_price <= pos["stop_price"]:
            msg = (f"🚨 STOP HIT — {pos['symbol']}\nLive price ₹{live_price:.2f} at/below "
                   f"stop ₹{pos['stop_price']}.\n\nReply SOLD once you've exited, or HOLD to override.")
            pos["status"] = "awaiting_sell_confirm"
            send_and_track(msg, {"type": "sell_confirm", "symbol": pos["symbol"]})
        updated.append(pos)
    return updated


# ── Job 2: deadline sweep ──
def deadline_sweep(now_ist, positions):
    ids = get_pending_index()
    for mid in ids:
        p = get_pending(mid)
        if not p:
            continue
        created = to_ist(p["created"])
        elapsed_min = (now_ist - created).total_seconds() / 60

        if p["type"] == "buy_intent":
            if now_ist.time() >= BUY_DEADLINE and created.date() == now_ist.date():
                positions = [x for x in positions if x["symbol"] != p["symbol"]]
                clear_pending(mid)
                send_telegram(f"⏰ No reply on {p['symbol']} by 7 PM — treating as SKIPPED.")

        elif p["type"] == "buy_price":
            if elapsed_min >= 60:
                positions = [x for x in positions if x["symbol"] != p["symbol"]]
                clear_pending(mid)
                send_telegram(f"⏰ No fill price for {p['symbol']} within 1hr — treating as SKIPPED, slot freed.")

        elif p["type"] == "sell_confirm":
            stage = p.get("reminders_sent", 0)
            last_action = to_ist(p.get("last_action_ts", p["created"]))
            since_last = (now_ist - last_action).total_seconds() / 60

            if stage == 0 and elapsed_min >= 60:
                send_telegram(f"⏰ Reminder — still waiting on {p['symbol']}. Reply SOLD or HOLD.")
                p["reminders_sent"] = 1; p["last_action_ts"] = int(now_ist.timestamp() * 1000)
                set_pending(mid, p)
            elif stage == 1 and since_last >= 30 and now_ist.time() >= REMIND2_TIME:
                send_telegram(f"⏰ Reminder (2) — still waiting on {p['symbol']}. Reply SOLD or HOLD.")
                p["reminders_sent"] = 2; p["last_action_ts"] = int(now_ist.timestamp() * 1000)
                set_pending(mid, p)
            elif stage == 2 and since_last >= 30 and now_ist.time() >= REMIND3_TIME:
                send_telegram(f"⏰ Final reminder today — still waiting on {p['symbol']}. Reply SOLD or HOLD.")
                p["reminders_sent"] = 3; p["last_action_ts"] = int(now_ist.timestamp() * 1000)
                set_pending(mid, p)
            elif stage == 3 and since_last >= 30 and now_ist.time() >= GIVEUP_TIME:
                clear_pending(mid)
                for x in positions:
                    if x["symbol"] == p["symbol"]:
                        x["status"] = "open"
                send_telegram(f"No reply on {p['symbol']} today — assuming still open. I'll ask again tomorrow.")

    return positions


# ── Job 3: send the delayed buy-price question at/after 12:45 ──
def send_delayed_price_prompts(now_ist, positions):
    if now_ist.time() < REMIND2_TIME:
        return
    for pos in positions:
        if pos.get("status") == "awaiting_price" and not pos.get("price_prompt_sent"):
            msg = f"Did you buy {pos['symbol']} at open? Reply your fill price, or NO if you didn't take it."
            send_and_track(msg, {"type": "buy_price", "symbol": pos["symbol"]})
            pos["price_prompt_sent"] = True


def main():
    now_ist = datetime.datetime.now(IST)
    if now_ist.weekday() >= 5:
        return

    positions = get_positions()
    positions = stop_loss_check(now_ist, positions)
    positions = deadline_sweep(now_ist, positions)
    send_delayed_price_prompts(now_ist, positions)
    save_positions(positions)


if __name__ == "__main__":
    main()
