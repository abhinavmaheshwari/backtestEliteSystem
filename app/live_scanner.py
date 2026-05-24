# =====================================================================================
# app/live_scanner.py
# =====================================================================================

import os
import pandas as pd
import yfinance as yf

from datetime import datetime, time

from technical_indicators import apply_indicators
from breakout_engine import detect_breakouts
from scoring_engine import calculate_score
from telegram_engine import send_telegram_message
from database import init_db, alert_exists, save_alert

from config import WATCHLIST_PATH

# =====================================================================================
# INITIALIZE DATABASE
# =====================================================================================

init_db()

# =====================================================================================
# MARKET HOURS FILTER
# =====================================================================================

current_time = datetime.now().time()

if not (

    time(9, 15)

    <= current_time

    <= time(15, 30)
):

    print("⏰ Market Closed")

    exit()

# =====================================================================================
# LOAD WATCHLIST
# =====================================================================================

try:

    watchlist = pd.read_parquet(
        WATCHLIST_PATH
    )

except Exception as e:

    print(f"❌ WATCHLIST LOAD ERROR -> {e}")

    print("🚀 RUNNING DAILY BUILDER...")

    result = os.system(
        "python app/daily_builder.py"
    )

    if result != 0:

        print("❌ DAILY BUILDER FAILED")

        exit()

    watchlist = pd.read_parquet(
        WATCHLIST_PATH
    )

print(f"\n🚀 SCANNING {len(watchlist)} STOCKS...\n")

# =====================================================================================
# ALERT COUNTER
# =====================================================================================

total_alerts = 0

# =====================================================================================
# MAIN LOOP
# =====================================================================================

for _, row in watchlist.iterrows():

    try:

        symbol = row["Stock"]

        category = row["Category"]

        print(f"🔍 Checking: {symbol}")

        # ============================================================================
        # DOWNLOAD DATA
        # ============================================================================

        ticker = yf.download(

            f"{symbol}.NS",

            period="1y",

            interval="1d",

            progress=False,

            auto_adjust=True
        )

        if ticker.empty:

            print(f"❌ No Data: {symbol}")

            continue

        ticker.reset_index(inplace=True)

        # ============================================================================
        # TECHNICALS
        # ============================================================================

        ticker = apply_indicators(
            ticker
        )

        signals = detect_breakouts(
            ticker
        )

        if len(signals) == 0:

            continue

        latest = ticker.iloc[-1]

        # ============================================================================
        # RSI SAFETY
        # ============================================================================

        if pd.isna(latest["RSI"]):

            continue

        # ============================================================================
        # VOLUME
        # ============================================================================

        latest_volume = latest["Volume"]

        avg_volume = (

            ticker["Volume"]

            .tail(10)

            .mean()
        )

        if avg_volume <= 0:

            continue

        volume_ratio = (

            latest_volume

            / avg_volume
        )

        # ============================================================================
        # STRONG BREAKOUT CANDLE
        # ============================================================================

        candle_range = (

            latest["High"]

            - latest["Low"]
        )

        candle_body = abs(

            latest["Close"]

            - latest["Open"]
        )

        if candle_range <= 0:

            continue

        body_ratio = (

            candle_body

            / candle_range
        )

        if body_ratio < 0.5:

            continue

        # ============================================================================
        # FILTERS
        # ============================================================================

        if volume_ratio < 1.5:

            continue

        if latest["RSI"] < 55:

            continue

        if latest["RSI"] > 85:

            continue

        if latest["Close"] < latest["EMA20"]:

            continue

        if latest["Close"] < latest["SMA50"]:

            continue

        if latest["SMA50"] < latest["SMA200"]:

            continue

        # ============================================================================
        # BREAKOUT TYPE
        # ============================================================================

        breakout_type = ", ".join(
            signals
        )

        # ============================================================================
        # DUPLICATE CHECK
        # ============================================================================

        if alert_exists(

            symbol,

            breakout_type
        ):

            print(
                f"⚠️ Duplicate skipped: {symbol}"
            )

            continue

        # ============================================================================
        # SCORE
        # ============================================================================

        score = calculate_score(

            category=category,

            breakout_count=len(signals),

            rsi=latest["RSI"],

            volume_ratio=volume_ratio
        )

        if score < 70:

            print(
                f"❌ Weak setup: {symbol} | Score={score}"
            )

            continue

        # ============================================================================
        # MESSAGE
        # ============================================================================

        message = f'''
🚀 ELITE BREAKOUT ALERT

Stock: {symbol}

Category:
{category}

Breakouts:
{breakout_type}

Price:
₹{round(latest["Close"], 2)}

RSI:
{round(latest["RSI"], 2)}

Volume Expansion:
{round(volume_ratio, 2)}x

Trend Structure:
✅ Above 20 EMA
✅ Above 50 DMA
✅ Bullish 50/200 DMA

Breakout Score:
{score}/100

Time:
{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
'''

        # ============================================================================
        # TELEGRAM ALERT
        # ============================================================================

        send_telegram_message(
            message
        )

        # ============================================================================
        # SAVE ALERT
        # ============================================================================

        save_alert(

            symbol,

            breakout_type,

            datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        )

        total_alerts += 1

        print(f"✅ ALERT SENT: {symbol}")

    except Exception as e:

        print(f"❌ ERROR: {symbol} -> {e}")

# =====================================================================================
# SUMMARY
# =====================================================================================

print(f"\n✅ TOTAL ALERTS SENT: {total_alerts}")

print("\n✅ SCAN COMPLETED\n")
