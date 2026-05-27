# =====================================================================================
# app/breakout_engine.py
# =====================================================================================

import numpy as np


def detect_breakouts(df):

    latest   = df.iloc[-1]
    signals  = []

    # ============================================================================
    # DAILY BREAKOUT  — close > 20-day high (excluding today)
    # ============================================================================

    prev_20d_high = df["High"].rolling(20).max().iloc[-2]
    if latest["Close"] > prev_20d_high:
        signals.append("Daily Breakout")

    # ============================================================================
    # WEEKLY BREAKOUT — close > 50-day high (excluding today)
    # ============================================================================

    prev_50d_high = df["High"].rolling(50).max().iloc[-2]
    if latest["Close"] > prev_50d_high:
        signals.append("Weekly Breakout")

    # ============================================================================
    # MONTHLY BREAKOUT — close > 100-day high (excluding today)
    # ============================================================================

    prev_100d_high = df["High"].rolling(100).max().iloc[-2]
    if latest["Close"] > prev_100d_high:
        signals.append("Monthly Breakout")

    # ============================================================================
    # 52-WEEK BREAKOUT — close > 252-day high (excluding today)
    # ============================================================================

    prev_252d_high = df["High"].rolling(252).max().iloc[-2]
    if latest["Close"] > prev_252d_high:
        signals.append("52W Breakout")

    return signals
