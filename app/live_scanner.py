# =====================================================================================
# app/live_scanner.py
# =====================================================================================

import pandas as pd
import yfinance as yf
import time

from datetime import datetime, time as dt_time

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
# WAIT FOR MARKET HOURS
# =====================================================================================

while True:

    current_time = datetime.now().time()

    weekday = datetime.now().weekday()

    market_open = (

        dt_time(9, 15)

        <= current_time

        <= dt_time(15, 30)
    )

    weekday_open = weekday < 5

    if market_open and weekday_open:

        break

    print("⏰ Market closed. Sleeping for 5 minutes...")

    time.sleep(300)

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

    from daily_builder import main as build_watchlist

    build_watchlist()

    watchlist = pd.read_parquet(
        WATCHLIST_PATH
    )

# =====================================================================================
# LIVE SCANNER START BANNER
# =====================================================================================

print("\n" + "=" * 80)

print("🚀 LIVE BREAKOUT SCANNER STARTED")

print(
    f"⏰ Scan Time: "
    f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
)

print(
    f"📊 Total Stocks In Watchlist: "
    f"{len(watchlist)}"
)

print("=" * 80 + "\n")

# =====================================================================================
# ALERT COUNTER
# =====================================================================================

total_alerts = 0

# =====================================================================================
# MAIN LOOP
# =====================================================================================

for _, row in watchlist.iterrows():

    try:

        # ============================================================================
        # STOCK INFO
        # ============================================================================

        symbol = row["Stock"]

        category = row["Category"]

        print(f"🔍 Checking: {symbol}")

        # ============================================================================
        # DOWNLOAD PRICE DATA
        # ============================================================================

        ticker = yf.download(

            f"{symbol}.NS",

            period="1y",

            interval="1d",

            progress=False,

            auto_adjust=True
        )

        if ticker.empty:

            print(f"❌ No data: {symbol}")

            continue

        # ============================================================================
        # RESET INDEX
        # ============================================================================

        ticker.reset_index(inplace=True)

        # ============================================================================
        # FIX YFINANCE MULTI-INDEX COLUMNS
        # ============================================================================

        if isinstance(ticker.columns, pd.MultiIndex):

            ticker.columns = ticker.columns.get_level_values(0)

        # ============================================================================
        # APPLY TECHNICAL INDICATORS
        # ============================================================================

        ticker = apply_indicators(
            ticker
        )

        # ============================================================================
        # DETECT BREAKOUTS
        # ============================================================================

        signals = detect_breakouts(
            ticker
        )

        if len(signals) == 0:

            continue

        # ============================================================================
        # LATEST CANDLE
        # ============================================================================

        latest = ticker.iloc[-1]

        # ============================================================================
        # RSI SAFETY
        # ============================================================================

        if pd.isna(latest["RSI"]):

            continue

        # ============================================================================
        # VOLUME ANALYSIS
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
        # STRONG BREAKOUT CANDLE FILTER
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

        # WEAK CANDLE REJECTION
        if body_ratio < 0.5:

            continue

        # ============================================================================
        # FAKE BREAKOUT FILTERS
        # ============================================================================

        # VOLUME EXPANSION REQUIRED
        if volume_ratio < 1.5:

            continue

        # HEALTHY RSI
        if latest["RSI"] < 55:

            continue

        # AVOID OVEREXTENDED MOVES
        if latest["RSI"] > 85:

            continue

        # ABOVE 20 EMA
        if latest["Close"] < latest["EMA20"]:

            continue

        # ABOVE 50 DMA
        if latest["Close"] < latest["SMA50"]:

            continue

        # BULLISH TREND STRUCTURE
        if latest["SMA50"] < latest["SMA200"]:

            continue

        # ============================================================================
        # BREAKOUT TYPE
        # ============================================================================

        breakout_type = ", ".join(
            signals
        )

        # ============================================================================
        # AVOID DUPLICATE ALERTS
        # ============================================================================

        if alert_exists(

            symbol,

            breakout_type
        ):

            print(f"⚠️ Duplicate skipped: {symbol}")

            continue

        # ============================================================================
        # CALCULATE SCORE
        # ============================================================================

        score = calculate_score(

            category=category,

            breakout_count=len(signals),

            rsi=latest["RSI"],

            volume_ratio=volume_ratio
        )

        # ============================================================================
        # MINIMUM SCORE FILTER
        # ============================================================================

        if score < 70:

            print(

                f"❌ Weak setup skipped: "

                f"{symbol} | Score={score}"
            )

            continue

        # ============================================================================
        # ALERT MESSAGE
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
        # SEND TELEGRAM ALERT
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

    # ============================================================================
    # ERROR HANDLING
    # ============================================================================

    except Exception as e:

        print(f"❌ ERROR: {symbol} -> {e}")

# =====================================================================================
# FINAL SUMMARY
# =====================================================================================

print("\n" + "=" * 80)

print(f"✅ TOTAL ALERTS SENT: {total_alerts}")

print(
    f"🏁 Scan Finished At: "
    f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
)

print("✅ LIVE SCANNER COMPLETED")

print("=" * 80 + "\n")
