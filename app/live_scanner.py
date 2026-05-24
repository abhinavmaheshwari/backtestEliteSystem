# =====================================================================================
# app/live_scanner.py
# =====================================================================================

import pandas as pd
import yfinance as yf

from datetime import datetime

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
# LOAD WATCHLIST
# =====================================================================================

watchlist = pd.read_parquet(WATCHLIST_PATH)

print(f"\n🚀 SCANNING {len(watchlist)} STOCKS...\n")

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
        # APPLY TECHNICAL INDICATORS
        # ============================================================================

        ticker = apply_indicators(ticker)

        # ============================================================================
        # DETECT BREAKOUTS
        # ============================================================================

        signals = detect_breakouts(ticker)

        if len(signals) == 0:

            continue

        # ============================================================================
        # LATEST CANDLE
        # ============================================================================

        latest = ticker.iloc[-1]

        # ============================================================================
        # VOLUME ANALYSIS
        # ============================================================================

        latest_volume = latest["Volume"]

        avg_volume = ticker["Volume"].tail(10).mean()

        if avg_volume <= 0:

            continue

        volume_ratio = latest_volume / avg_volume

        # ============================================================================
        # FAKE BREAKOUT FILTERS
        # ============================================================================

        # VOLUME EXPANSION REQUIRED
        if volume_ratio < 1.5:

            continue

        # HEALTHY RSI
        if latest["RSI"] < 55:

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

        breakout_type = ", ".join(signals)

        # ============================================================================
        # AVOID DUPLICATE ALERTS
        # ============================================================================

        if alert_exists(symbol, breakout_type):

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

            print(f"❌ Weak setup skipped: {symbol} | Score={score}")

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

        send_telegram_message(message)

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

        print(f"✅ ALERT SENT: {symbol}")

    # ============================================================================
    # ERROR HANDLING
    # ============================================================================

    except Exception as e:

        print(f"❌ ERROR: {symbol} -> {e}")

# =====================================================================================

print("\n✅ SCAN COMPLETED\n")
