f"""
SwingTrader — Real Daily Scanner (Phase 4)
============================================
Runs at 3:35 PM IST on weekdays (skips NSE holidays automatically).
Does two jobs:
  1. Checks every OPEN position for a real exit signal (RSI momentum-unlock
     + red candle, or 20-day time-bail) and sends a holding-check /
     sell-alert message via Telegram.
  2. If slots are free and the market is bullish, scans your elite universe
     for new entries and sends buy-signal messages.

All state (positions, trade log, pending replies) lives in the same
Cloudflare KV store your Telegram bot (the Worker) reads and writes —
so replying YES/NO/SOLD/KEEP in Telegram continues the conversation
this script starts.
"""

import os
import sys
import json
import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
import ta
import requests


# ═══════════════════════════ CONFIG ═══════════════════════════
BUY_DIP_RSI       = 38
BUY_CAPIT_RSI     = 15
MIN_PULLBACK_PCT  = 4.0
PULLBACK_WINDOW   = 10
SELL_RSI          = 75     # exit unlocks once RSI_3 >= this
MAX_HOLD_DAYS     = 20     # time-bail if momentum never unlocks

TOTAL_CAPITAL     = 16_000
MAX_SLOTS         = 2
BASE_SLOT         = TOTAL_CAPITAL / MAX_SLOTS
TIER1_ALLOC       = int(BASE_SLOT * 1.5)
TIER2_ALLOC       = int(BASE_SLOT * 0.75)
TIER1_THRESHOLD   = 2.5

ELITE_SYMBOLS_FILE = os.path.join(os.path.dirname(__file__), "elite_symbols.json")
IST = ZoneInfo("Asia/Kolkata")

# NSE 2026 trading holidays (weekday closures only — source: NSE India)
NSE_HOLIDAYS_2026 = {
    "2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31",
    "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26",
    "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-10", "2026-11-24",
    "2026-12-25",
}

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
CF_ACCOUNT_ID      = os.environ["CF_ACCOUNT_ID"]
CF_KV_NAMESPACE_ID = os.environ["CF_KV_NAMESPACE_ID"]
CF_API_TOKEN       = os.environ["CF_API_TOKEN"]

KV_URL = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}"
KV_HEADERS = {"Authorization": f"Bearer {CF_API_TOKEN}"}


# ═══════════════════════════ KV STORAGE (mirrors the Worker exactly) ═══════════════════════════
def kv_get(key, default=None):
    r = requests.get(f"{KV_URL}/values/{key}", headers=KV_HEADERS, timeout=15)
    if r.status_code == 404:
        return default
    r.raise_for_status()
    return r.text


def kv_put(key, value):
    r = requests.put(f"{KV_URL}/values/{key}", headers=KV_HEADERS, data=value, timeout=15)
    r.raise_for_status()


def kv_delete(key):
    r = requests.delete(f"{KV_URL}/values/{key}", headers=KV_HEADERS, timeout=15)
    if r.status_code not in (200, 404):
        r.raise_for_status()


def get_positions():
    raw = kv_get("positions", "[]")
    return json.loads(raw)


def save_positions(positions):
    kv_put("positions", json.dumps(positions))


def get_trade_log():
    raw = kv_get("trade_log", "[]")
    return json.loads(raw)


def get_pending_index():
    raw = kv_get("pending_index", "[]")
    return json.loads(raw)


def save_pending_index(ids):
    kv_put("pending_index", json.dumps(ids))


def set_pending(message_id, data):
    payload = {**data, "created": int(datetime.datetime.now().timestamp() * 1000)}
    kv_put(f"pending:{message_id}", json.dumps(payload))
    ids = get_pending_index()
    if str(message_id) not in ids:
        ids.append(str(message_id))
        save_pending_index(ids)


# ═══════════════════════════ TELEGRAM ═══════════════════════════
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)
    r.raise_for_status()
    result = r.json().get("result")
    return result["message_id"] if result else None


def send_and_track(text, pending_data):
    message_id = send_telegram(text)
    if message_id:
        set_pending(message_id, pending_data)
    return message_id


# ═══════════════════════════ INDICATORS ═══════════════════════════
def compute_indicators(df):
    df = df.copy()
    if len(df) < 200:
        return None
    c, h, l = df["Close"], df["High"], df["Low"]
    df["SMA_200"]     = c.rolling(200).mean()
    df["EMA_50"]      = ta.trend.EMAIndicator(c, window=50).ema_indicator()
    df["RSI_3"]       = ta.momentum.RSIIndicator(c, window=3).rsi()
    df["ATR_14"]      = ta.volatility.AverageTrueRange(h, l, c, window=14).average_true_range()
    df["Recent_High"] = h.rolling(PULLBACK_WINDOW).max()
    return df


def fetch_daily(symbol_ns):
    raw = yf.download(symbol_ns, period="90d", interval="1d", progress=False, auto_adjust=True)
    if raw.empty or len(raw) < 30:
        return None
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    return raw.dropna()


def load_elite_universe():
    if not os.path.exists(ELITE_SYMBOLS_FILE):
        print(f"❌ {ELITE_SYMBOLS_FILE} not found.")
        sys.exit(1)
    with open(ELITE_SYMBOLS_FILE) as f:
        return pd.DataFrame(json.load(f))  # columns: symbol, ret_per_trade, avg_hold_days


# ═══════════════════════════ EXIT CHECK (per open position) ═══════════════════════════
def check_exit_conditions(pos, today_ist):
    """Returns (reason_or_None, updated_momentum_unlocked)."""
    raw = fetch_daily(pos["symbol"] + ".NS")
    if raw is None:
        return None, pos.get("momentum_unlocked", False)
    df = compute_indicators(raw)
    if df is None:
        return None, pos.get("momentum_unlocked", False)
    df = df.dropna(subset=["RSI_3"])
    if df.empty:
        return None, pos.get("momentum_unlocked", False)

    today_row = df.iloc[-1]
    unlocked = pos.get("momentum_unlocked", False)
    if today_row["RSI_3"] >= SELL_RSI:
        unlocked = True
    red_candle = today_row["Close"] < today_row["Open"]

    entry_date = datetime.date.fromisoformat(pos["entry_date"])
    days_held = (today_ist - entry_date).days

    if unlocked and red_candle:
        return (f"RSI momentum exit — RSI₃ reached ≥{SELL_RSI} during the hold, "
                f"red candle today. Sell at tomorrow's open."), unlocked
    if days_held >= MAX_HOLD_DAYS:
        return (f"Time-bail — held {days_held} days (limit {MAX_HOLD_DAYS}), "
                f"momentum never unlocked. Sell at tomorrow's open."), unlocked
    return None, unlocked


# ═══════════════════════════ ENTRY SCAN ═══════════════════════════
def scan_for_signals(elite_df, held_symbols):
    max_rpt = elite_df["ret_per_trade"].max()
    min_rpt = elite_df["ret_per_trade"].min()
    candidates = []

    for _, row in elite_df.iterrows():
        symbol = str(row["symbol"]).replace(".NS", "")
        if symbol in held_symbols:
            continue
        raw = fetch_daily(symbol + ".NS")
        if raw is None:
            continue
        df = compute_indicators(raw)
        if df is None:
            continue
        df = df.dropna(subset=["RSI_3", "EMA_50", "SMA_200", "ATR_14", "Recent_High"])
        if len(df) < 2:
            continue

        today = df.iloc[-1]
        rsi3, close, open_ = today["RSI_3"], today["Close"], today["Open"]
        recent_high, ema50, sma200, atr = (
            today["Recent_High"], today["EMA_50"], today["SMA_200"], today["ATR_14"]
        )

        pullback_pct = (recent_high - close) / recent_high * 100
        green_candle = close > open_
        trend_ok = ema50 > sma200
        dip_ok = rsi3 <= BUY_DIP_RSI
        capit_ok = rsi3 <= BUY_CAPIT_RSI
        pullback_ok = pullback_pct >= MIN_PULLBACK_PCT

        if not (trend_ok and pullback_ok and ((dip_ok and green_candle) or capit_ok)):
            continue

        rpt = float(row["ret_per_trade"])
        avg_hold = float(row.get("avg_hold_days", 0))
        tier = "T1" if rpt >= TIER1_THRESHOLD else "T2"
        alloc = TIER1_ALLOC if tier == "T1" else TIER2_ALLOC

        edge_norm = (rpt - min_rpt) / (max_rpt - min_rpt + 1e-9)
        rsi_norm = max(0, min(1, (BUY_DIP_RSI - rsi3) / (BUY_DIP_RSI - 1 + 1e-9)))
        pull_norm = min(pullback_pct / 20.0, 1.0)
        score = round((edge_norm * 0.50 + rsi_norm * 0.30 + pull_norm * 0.20) * 100, 1)
        calc_stop = round(close - 3.0 * atr, 2)
        risk_pct = round((close - calc_stop) / close * 100, 2)

        candidates.append({
            "symbol": symbol, "tier": tier, "score": score,
            "refClose": round(close, 2), "calcStop": calc_stop, "alloc": alloc,
            "rsi3": round(rsi3, 1), "pullback_pct": round(pullback_pct, 1),
            "risk_pct": risk_pct, "avg_hold": round(avg_hold),
            "signal_type": "Capitulation" if capit_ok else "Dip",
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates


# ═══════════════════════════ MAIN ═══════════════════════════
def main():
    now_ist = datetime.datetime.now(IST)
    today = now_ist.date()

    if today.weekday() >= 5:
        print("Weekend — skipping.")
        return
    if today.isoformat() in NSE_HOLIDAYS_2026:
        send_telegram(f"📅 {today.strftime('%d %b %Y')} is an NSE trading holiday — no scan today.")
        return

    positions = get_positions()
    elite_df = load_elite_universe()

    # ── 1. Daily exit check for every OPEN position ──
    updated_positions = []
    for pos in positions:
        if pos.get("status") != "open":
            updated_positions.append(pos)
            continue

        reason, unlocked = check_exit_conditions(pos, today)
        pos["momentum_unlocked"] = unlocked

        if reason:
            msg = f"🚨 SELL — {pos['symbol']}\nReason: {reason}\n\nReply SOLD once you've exited, or HOLD to keep it open."
        else:
            msg = (f"📊 Holding check — {pos['symbol']} (entry ₹{pos.get('entry_price','?')}, "
                   f"stop ₹{pos.get('stop_price','?')})\nStill in this position?\n\n"
                   f"Reply HOLD to confirm, or SOLD if you already exited on your own.")

        pos["status"] = "awaiting_sell_confirm"
        send_and_track(msg, {"type": "sell_confirm", "symbol": pos["symbol"]})
        updated_positions.append(pos)

    positions = updated_positions
    save_positions(positions)

    # ── 2. Entry scan, only if a slot is free ──
    free_slots = MAX_SLOTS - len(positions)
    if free_slots <= 0:
        held = ", ".join(p["symbol"] for p in positions)
        send_telegram(f"📡 Scan — {today.strftime('%d %b %Y')}\nBoth slots full ({held}) — skipping entry scan.")
        return

    nifty_raw = fetch_daily("^NSEI")
    if nifty_raw is None:
        send_telegram("⚠️ Couldn't fetch Nifty data today — scan skipped.")
        return
    nifty_ema = ta.trend.EMAIndicator(nifty_raw["Close"], window=50).ema_indicator()
    nifty_close = float(nifty_raw["Close"].iloc[-1])
    nifty_ema50 = float(nifty_ema.iloc[-1])
    market_bullish = nifty_close > nifty_ema50

    if not market_bullish:
        send_telegram(f"📡 Scan — {today.strftime('%d %b %Y')}\nNifty {nifty_close:.0f} below EMA50 {nifty_ema50:.0f} — no new entries today.")
        return

    held_symbols = {p["symbol"] for p in positions}
    candidates = scan_for_signals(elite_df, held_symbols)
    chosen = candidates[:free_slots]

    if not chosen:
        send_telegram(f"📡 Scan — {today.strftime('%d %b %Y')}\nMarket bullish (Nifty {nifty_close:.0f} > EMA50 {nifty_ema50:.0f}). No qualifying signals today.")
        return

    for c in chosen:
        position = {
            "symbol": c["symbol"], "status": "awaiting_intent",
            "tier": c["tier"], "score": c["score"],
            "refClose": c["refClose"], "calcStop": c["calcStop"], "alloc": c["alloc"],
        }
        positions.append(position)
        msg = (f"📈 BUY SIGNAL — {c['symbol']} ({c['tier']}, {c['signal_type']}, score {c['score']}/100)\n"
               f"Ref price: ₹{c['refClose']}  |  Suggested stop: ₹{c['calcStop']}  |  Alloc: ₹{c['alloc']:,}\n"
               f"RSI₃ {c['rsi3']}  |  Pullback {c['pullback_pct']}%  |  Risk {c['risk_pct']}%  |  ~{c['avg_hold']}d avg hold\n\n"
               f"Take this trade? Reply YES or NO to THIS message.")
        send_and_track(msg, {"type": "buy_intent", "symbol": c["symbol"]})

    save_positions(positions)


if __name__ == "__main__":
    main()
