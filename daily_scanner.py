"""
SwingTrader — Headless Daily Scanner
=====================================
This is Cell 9's "SECTION 3: SCAN FOR NEW SIGNALS" logic from the
SwingTrader_Nifty50 notebook, extracted into a script that:

  - has NO input() calls (safe to run on a schedule / CI runner)
  - sends the result to your phone via Telegram instead of printing
    to a notebook cell you have to be sitting in front of

It intentionally does NOT touch position tracking / trades.json.
That still lives in your Colab notebook, because closing a trade
requires you to type your actual fill price by hand anyway — there's
nothing to automate there until you wire up real order execution.

This script's only job: make sure you get told about a signal on a
day you fired, even if you never open Colab.

────────────────────────────────────────────────────────────────────
SETUP (see SETUP.md for the full walkthrough):
  1. Export elite_symbols.json from your Colab notebook (Cell 7) —
     see the snippet in SETUP.md. Commit that file into this repo.
  2. Set GitHub repo secrets: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  3. (optional) Set a repo variable ACTIVE_SYMBOLS as a comma list
     of symbols you're currently holding, so the scanner skips them
     — e.g. "RELIANCE,TCS". Update it by hand when you open/close a
     position. Leave blank/unset if you don't want this.
────────────────────────────────────────────────────────────────────
"""

import json
import os
import sys
import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
import ta
import requests


# ───────────────────────── CONFIG (mirrors the notebook) ─────────────────────────
BUY_DIP_RSI       = 38
BUY_CAPIT_RSI     = 15
MIN_PULLBACK_PCT  = 4.0
PULLBACK_WINDOW   = 10

TOTAL_CAPITAL     = 16_000
MAX_CONCURRENT    = 2
BASE_SLOT         = TOTAL_CAPITAL / MAX_CONCURRENT
TIER1_ALLOC       = int(BASE_SLOT * 1.5)
TIER2_ALLOC       = int(BASE_SLOT * 0.75)
TIER1_THRESHOLD   = 2.5

ELITE_SYMBOLS_FILE = os.path.join(os.path.dirname(__file__), "elite_symbols.json")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
ACTIVE_SYMBOLS     = [s.strip() for s in os.environ.get("ACTIVE_SYMBOLS", "").split(",") if s.strip()]


# ───────────────────────── TELEGRAM ─────────────────────────
def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — printing instead:\n")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)
        if r.status_code != 200:
            print(f"⚠️  Telegram send failed: {r.status_code} {r.text}")
    except Exception as e:
        print(f"⚠️  Telegram send error: {e}")


# ───────────────────────── INDICATORS (same as notebook Cell 3) ─────────────────────────
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


def load_elite_universe():
    if not os.path.exists(ELITE_SYMBOLS_FILE):
        print(f"❌ {ELITE_SYMBOLS_FILE} not found. See SETUP.md — export it from Colab first.")
        sys.exit(1)
    with open(ELITE_SYMBOLS_FILE) as f:
        data = json.load(f)
    return pd.DataFrame(data)  # columns: symbol, ret_per_trade, avg_hold_days


def main():
    now_ist = datetime.datetime.now(ZoneInfo("Asia/Kolkata"))
    header = f"📡 SwingTrader Scan — {now_ist.strftime('%a %d %b %Y, %I:%M %p')} IST"

    elite_df = load_elite_universe()
    if elite_df.empty:
        send_telegram(f"{header}\n\n❌ elite_symbols.json is empty. Nothing to scan.")
        return

    # ── Market check ──
    nifty_raw = yf.download("^NSEI", period="60d", interval="1d", progress=False, auto_adjust=True)
    if isinstance(nifty_raw.columns, pd.MultiIndex):
        nifty_raw.columns = nifty_raw.columns.get_level_values(0)
    nifty_ema = ta.trend.EMAIndicator(nifty_raw["Close"], window=50).ema_indicator()
    nifty_close = float(nifty_raw["Close"].iloc[-1])
    nifty_ema50 = float(nifty_ema.iloc[-1])
    market_bullish = nifty_close > nifty_ema50

    lines = [header, f"Nifty: {nifty_close:.0f} | EMA50: {nifty_ema50:.0f} | "
             f"{'🟢 Bullish' if market_bullish else '🔴 Bearish'}"]

    if not market_bullish:
        lines.append("\n⛔ Market below EMA50 — no scan run today.")
        send_telegram("\n".join(lines))
        return

    # ── Scan elite universe ──
    triggered = []
    for _, row in elite_df.iterrows():
        sym_clean = str(row["symbol"]).replace(".NS", "")
        if sym_clean in ACTIVE_SYMBOLS:
            continue
        try:
            raw = yf.download(sym_clean + ".NS", period="60d", interval="1d",
                               progress=False, auto_adjust=True)
            if raw.empty or len(raw) < 30:
                continue
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = raw.columns.get_level_values(0)
            raw = raw.dropna()
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

            if trend_ok and pullback_ok and ((dip_ok and green_candle) or capit_ok):
                rpt = float(row["ret_per_trade"])
                avg_hold = float(row.get("avg_hold_days", 0))
                tier = "T1" if rpt >= TIER1_THRESHOLD else "T2"
                alloc = TIER1_ALLOC if tier == "T1" else TIER2_ALLOC

                max_rpt = elite_df["ret_per_trade"].max()
                min_rpt = elite_df["ret_per_trade"].min()
                edge_norm = (rpt - min_rpt) / (max_rpt - min_rpt + 1e-9)
                rsi_norm = max(0, min(1, (BUY_DIP_RSI - rsi3) / (BUY_DIP_RSI - 1 + 1e-9)))
                pull_norm = min(pullback_pct / 20.0, 1.0)
                score = round((edge_norm * 0.50 + rsi_norm * 0.30 + pull_norm * 0.20) * 100, 1)
                stop = round(close - 3.0 * atr, 2)
                risk_pct = round((close - stop) / close * 100, 2)

                triggered.append({
                    "symbol": sym_clean, "tier": tier, "score": score,
                    "close": round(close, 2), "rsi3": round(rsi3, 1),
                    "pullback_pct": round(pullback_pct, 1), "stop": stop,
                    "risk_pct": risk_pct, "alloc": alloc, "avg_hold": round(avg_hold),
                    "signal_type": "🩸 Capit" if capit_ok else "🔥 Dip",
                })
        except Exception:
            continue

    if not triggered:
        lines.append("\nNo signals today.")
        send_telegram("\n".join(lines))
        return

    tdf = pd.DataFrame(triggered).sort_values("score", ascending=False).reset_index(drop=True)
    lines.append(f"\n✅ {len(tdf)} signal(s):\n")
    for idx, r in tdf.iterrows():
        lines.append(
            f"{idx+1}. {r['symbol']} ({r['tier']}, {r['signal_type']}) — score {r['score']}/100\n"
            f"   Price ₹{r['close']}  |  RSI3 {r['rsi3']}  |  Pullback {r['pullback_pct']}%\n"
            f"   Stop ₹{r['stop']} ({r['risk_pct']}% risk)  |  Alloc ₹{r['alloc']:,}  |  "
            f"~{r['avg_hold']:.0f}d hold\n"
        )
    lines.append("Entry rule: buy at open tomorrow if you take it. Log it in the Colab tracker.")
    send_telegram("\n".join(lines))


if __name__ == "__main__":
    main()
