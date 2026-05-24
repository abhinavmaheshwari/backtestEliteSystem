import pandas as pd
import yfinance as yf

from datetime import datetime

from technical_indicators import apply_indicators
from breakout_engine import detect_breakouts
from scoring_engine import calculate_score
from telegram_engine import send_telegram_message
from database import init_db, alert_exists, save_alert

from config import WATCHLIST_PATH


init_db()


watchlist = pd.read_parquet(WATCHLIST_PATH)


for _, row in watchlist.iterrows():

    try:

        symbol = row["Stock"]
        category = row["Category"]

        ticker = yf.download(
            f"{symbol}.NS",
            period="1y",
            interval="1d",
            progress=False
        )

        if ticker.empty:
            continue

        ticker.reset_index(inplace=True)

        ticker = apply_indicators(ticker)

        signals = detect_breakouts(ticker)

        if len(signals) == 0:
            continue

        latest = ticker.iloc[-1]

        latest_volume = latest["Volume"]

        avg_volume = ticker["Volume"].tail(10).mean()

        volume_ratio = latest_volume / avg_volume

        # FAKE BREAKOUT FILTERS

        if volume_ratio < 1.5:
            continue

        if latest["RSI"] < 55:
            continue

        if latest["Close"] < latest["EMA20"]:
            continue

        if latest["Close"] < latest["SMA50"]:
            continue

        if latest["SMA50"] < latest["SMA200"]:
            continue

        breakout_type = ", ".join(signals)

        if alert_exists(symbol, breakout_type):
            continue

        score = calculate_score(
        print(f"ERROR: {symbol} -> {e}")
