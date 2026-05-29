# =====================================================================================
# app/breakout_engine.py
# =====================================================================================

import numpy as np
import pandas as pd


def detect_breakouts(df: pd.DataFrame) -> list[str]:
    """
    Detects breakout signals on the latest completed candle.

    Returns a list of signal name strings. Returns [] if data is
    insufficient or the latest close is NaN.

    Signals:
        "Daily Breakout"   — close > 20-bar rolling high (excl. today)
        "Weekly Breakout"  — close > 50-bar rolling high (excl. today)
        "Monthly Breakout" — close > 100-bar rolling high (excl. today)
        "52W Breakout"     — close > 252-bar rolling high (excl. today)
    """

    if df is None or df.empty:
        return []

    latest = df.iloc[-1]
    signals = []

    close = latest["Close"]
    if pd.isna(close):
        return []

    # ── DAILY BREAKOUT — close > 20-bar high (excl. today) ───────────────────────
    # Need at least 21 rows: 20 for the rolling window + the current bar
    if len(df) >= 21:
        prev_20d_high = df["High"].rolling(20).max().iloc[-2]
        if not pd.isna(prev_20d_high) and close > prev_20d_high:
            signals.append("Daily Breakout")

    # ── WEEKLY BREAKOUT — close > 50-bar high (excl. today) ──────────────────────
    # Need at least 51 rows
    if len(df) >= 51:
        prev_50d_high = df["High"].rolling(50).max().iloc[-2]
        if not pd.isna(prev_50d_high) and close > prev_50d_high:
            signals.append("Weekly Breakout")

    # ── MONTHLY BREAKOUT — close > 100-bar high (excl. today) ────────────────────
    # Need at least 101 rows.
    # On 15m/1H timeframes (5d/60d of data), this window is never populated —
    # that's expected. The signal simply won't fire on those shorter datasets.
    if len(df) >= 101:
        prev_100d_high = df["High"].rolling(100).max().iloc[-2]
        if not pd.isna(prev_100d_high) and close > prev_100d_high:
            signals.append("Monthly Breakout")

    # ── 52W BREAKOUT — close > 252-bar high (excl. today) ────────────────────────
    # Requires daily data with 1y history (eod_scanner). Will never fire on intraday
    # or 1H data — that's by design.
    if len(df) >= 253:
        prev_252d_high = df["High"].rolling(252).max().iloc[-2]
        if not pd.isna(prev_252d_high) and close > prev_252d_high:
            signals.append("52W Breakout")

    return signals
