# =====================================================================================
# app/breakout_engine.py
#
# SIGNAL DESIGN RATIONALE BY TIMEFRAME
#
# Daily (EOD) — 252 bars of daily data available:
#   "Daily Breakout"    close > 20-bar high  = above 1-month high
#   "Weekly Breakout"   close > 50-bar high  = above ~10-week high
#   "Monthly Breakout"  close > 100-bar high = above ~5-month high
#   "52W Breakout"      close > 252-bar high = new 52-week high
#
# 1H — ~360 bars (60 days × 6 bars/day):
#   "Hourly Breakout"   close > 6-bar high   = above prior full trading day
#   "Daily Breakout"    close > 26-bar high  = above prior ~5 trading days
#   "Weekly Breakout"   close > 130-bar high = above prior ~25 days (5 weeks)
#   "Monthly Breakout"  close > 260-bar high = above prior ~50 days (10 weeks)
#
# 15m — ~130 bars (5 days × 26 bars/day):
#   "Session Breakout"  close > 26-bar high  = above prior full trading day
#   "Daily Breakout"    close > 52-bar high  = above prior 2 trading days
#   "Weekly Breakout"   close > 104-bar high = above prior 4 trading days
#
# The WINDOW_MAP below encodes these lookbacks per timeframe.
# All comparisons use .iloc[-2] to exclude the current bar (same as before).
# =====================================================================================

import pandas as pd


WINDOW_MAP = {
    "1d": [
        ("Daily Breakout",   20),
        ("Weekly Breakout",  50),
        ("Monthly Breakout", 100),
        ("52W Breakout",     252),
    ],
    "1h": [
        ("Hourly Breakout",  6),
        ("Daily Breakout",   26),
        ("Weekly Breakout",  130),
        ("Monthly Breakout", 260),
    ],
    "15m": [
        ("Session Breakout", 26),
        ("Daily Breakout",   52),
        ("Weekly Breakout",  104),
    ],
}


def detect_breakouts(df: pd.DataFrame, timeframe: str = "15m") -> list[str]:
    """
    Detects breakout signals on the latest completed candle.

    Parameters
    ----------
    df        : OHLCV DataFrame with indicators already applied
    timeframe : "15m" | "1h" | "1d" — controls which rolling windows are used

    Returns a list of signal name strings, e.g. ["Session Breakout", "Daily Breakout"].
    Returns [] if data is insufficient or the latest close is NaN.
    """

    if df is None or df.empty:
        return []

    latest = df.iloc[-1]
    close  = latest["Close"]

    if pd.isna(close):
        return []

    signals   = []
    windows   = WINDOW_MAP.get(timeframe, WINDOW_MAP["15m"])
    n         = len(df)

    for signal_name, window in windows:
        # Need window + 1 rows: `window` for the rolling calc, +1 for .iloc[-2]
        if n < window + 1:
            continue

        prev_high = df["High"].rolling(window).max().iloc[-2]

        if pd.isna(prev_high):
            continue

        if close > prev_high:
            signals.append(signal_name)

    # ── BOLLINGER BAND SQUEEZE BREAKOUT ──────────────────────────────────────────
    # Price closes above the upper BB band with bandwidth expanding.
    # Works on all timeframes — BB uses a fixed 20-bar window.
    if (
        "BB_UPPER" in df.columns and
        "BB_LOWER" in df.columns and
        n >= 22
    ):
        bb_upper   = df["BB_UPPER"].iloc[-1]
        bb_width   = df["BB_UPPER"].iloc[-1] - df["BB_LOWER"].iloc[-1]
        bb_width_5 = (df["BB_UPPER"].iloc[-6] - df["BB_LOWER"].iloc[-6]) if n >= 6 else None

        if (
            not pd.isna(bb_upper) and
            close > bb_upper and
            bb_width_5 is not None and
            not pd.isna(bb_width_5) and
            bb_width > bb_width_5   # bands expanding = breakout, not just overextension
        ):
            signals.append("BB Breakout")

    # ── VOLUME SURGE ──────────────────────────────────────────────────────────────
    # Current bar volume >= 3x 20-bar average AND price is up.
    # A volume surge this strong is itself a breakout signal regardless of price level.
    if "Volume" in df.columns and n >= 21:
        vol_now = float(df["Volume"].iloc[-1])
        vol_avg = float(df["Volume"].tail(20).mean())

        if vol_avg > 0 and vol_now >= 3.0 * vol_avg and close > float(df["Open"].iloc[-1]):
            signals.append("Volume Surge")

    return signals
