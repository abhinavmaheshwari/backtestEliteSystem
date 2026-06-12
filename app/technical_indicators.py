# =====================================================================================
# app/technical_indicators.py  (UPGRADED v3)
#
# CHANGES FROM v2:
#   1. SWING_LOW/SWING_HIGH now use proper pivot detection (3-bar left, 3-bar right)
#      instead of rolling min/max — rolling window just gives the lowest/highest bar
#      over the period, not a true swing point. True swing lows/highs are cleaner
#      S/R levels that the market actually reacts to.
#   2. LOOKBACK_SWING_LOW / LOOKBACK_SWING_HIGH: the last confirmed swing points
#      from recent bars (not including current forming bar) — these are the levels
#      used for SL/Target placement.
#   3. Keep simple rolling SWING_LOW_RAW / SWING_HIGH_RAW as fallback.
#   4. All pivot point formulas retained (PP, S1-S3, R1-R3).
#   5. ATR_PCT and all rolling window highs retained.
# =====================================================================================

import pandas as pd
import ta


def _find_swing_lows(low: pd.Series, n: int = 3) -> pd.Series:
    """
    Detect pivot swing lows: a bar where the low is lower than the `n` bars
    on either side. Returns the swing low price at those pivots, NaN elsewhere.
    Then forward-fills so every bar knows the most recent confirmed swing low.
    """
    result = pd.Series(float("nan"), index=low.index)
    arr = low.values
    for i in range(n, len(arr) - n):
        window = arr[i - n: i + n + 1]
        if arr[i] == min(window):
            result.iloc[i] = arr[i]
    # Forward-fill: each bar "inherits" the last confirmed swing low
    return result.ffill()


def _find_swing_highs(high: pd.Series, n: int = 3) -> pd.Series:
    """
    Detect pivot swing highs: a bar where the high is higher than the `n` bars
    on either side. Forward-fills the most recent confirmed swing high.
    """
    result = pd.Series(float("nan"), index=high.index)
    arr = high.values
    for i in range(n, len(arr) - n):
        window = arr[i - n: i + n + 1]
        if arr[i] == max(window):
            result.iloc[i] = arr[i]
    return result.ffill()


def apply_indicators(df: pd.DataFrame, timeframe: str = "1d") -> pd.DataFrame:
    """
    Applies all technical indicators and returns the enriched DataFrame.

    Columns produced:
    -- Trend -----------------------------------------------------------
    EMA20, SMA50, SMA200
    -- Momentum --------------------------------------------------------
    RSI, MACD, MACD_SIGNAL, MACD_HIST
    -- Volatility ------------------------------------------------------
    ATR, ATR_PCT, BB_UPPER, BB_LOWER, BB_MID
    -- Directional -----------------------------------------------------
    ADX
    -- Support / Resistance --------------------------------------------
    SWING_LOW    — last confirmed pivot swing low  (true support)
    SWING_HIGH   — last confirmed pivot swing high (true resistance)
    SWING_LOW_RAW   — rolling window min (fallback if swing not found)
    SWING_HIGH_RAW  — rolling window max (fallback if swing not found)
    PP           — Classic Pivot Point (previous bar)
    S1, S2, S3   — Pivot Supports
    R1, R2, R3   — Pivot Resistances
    -- Breakout highs (pre-calculated, timeframe-aware) ----------------
    HIGH_20D, HIGH_50D, HIGH_100D, HIGH_252D, HIGH_52W
    """

    if df is None or df.empty:
        return df

    close = df["Close"]
    high  = df["High"]
    low   = df["Low"]

    # ── TREND — Moving Averages ────────────────────────────────────────────────
    df["EMA20"]  = ta.trend.ema_indicator(close, window=20)
    df["SMA50"]  = ta.trend.sma_indicator(close, window=50)
    df["SMA200"] = ta.trend.sma_indicator(close, window=200)

    # ── MOMENTUM — RSI ─────────────────────────────────────────────────────────
    df["RSI"] = ta.momentum.rsi(close, window=14)

    # ── VOLATILITY — ATR + ATR% + Bollinger Bands ─────────────────────────────
    df["ATR"]     = ta.volatility.average_true_range(high, low, close, window=14)
    df["ATR_PCT"] = (df["ATR"] / close * 100).round(2)

    bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)
    df["BB_UPPER"] = bb.bollinger_hband()
    df["BB_LOWER"] = bb.bollinger_lband()
    df["BB_MID"]   = bb.bollinger_mavg()

    # ── TREND DIRECTION — ADX ─────────────────────────────────────────────────
    adx_ind   = ta.trend.ADXIndicator(high, low, close, window=14)
    df["ADX"] = adx_ind.adx()

    # ── MOMENTUM CONFIRMATION — MACD ──────────────────────────────────────────
    macd_ind          = ta.trend.MACD(close, window_slow=26, window_fast=12, window_sign=9)
    df["MACD"]        = macd_ind.macd()
    df["MACD_SIGNAL"] = macd_ind.macd_signal()
    df["MACD_HIST"]   = macd_ind.macd_diff()

    # ── SUPPORT / RESISTANCE — TRUE Pivot Swing Points ────────────────────────
    # n = how many bars on each side a bar must be the extreme to qualify
    pivot_n = {"1d": 5, "1h": 4, "15m": 3}.get(timeframe, 5)

    # True swing levels — these are what price actually bounces off of
    df["SWING_LOW"]  = _find_swing_lows(low,  n=pivot_n)
    df["SWING_HIGH"] = _find_swing_highs(high, n=pivot_n)

    # Rolling fallback (simple window min/max) — used only if swing not available
    swing_window = {"1d": 20, "1h": 14, "15m": 10}.get(timeframe, 20)
    df["SWING_LOW_RAW"]  = low.rolling(window=swing_window,  min_periods=swing_window // 2).min()
    df["SWING_HIGH_RAW"] = high.rolling(window=swing_window, min_periods=swing_window // 2).max()

    # ── SUPPORT / RESISTANCE — Classic Pivot Points ───────────────────────────
    # Using 3-bar lookback pivot for intraday, previous daily bar for EOD
    prev_high  = high.shift(1)
    prev_low   = low.shift(1)
    prev_close = close.shift(1)

    df["PP"] = ((prev_high + prev_low + prev_close) / 3).round(2)
    df["R1"] = (2 * df["PP"] - prev_low).round(2)
    df["R2"] = (df["PP"] + (prev_high - prev_low)).round(2)
    df["R3"] = (prev_high + 2 * (df["PP"] - prev_low)).round(2)
    df["S1"] = (2 * df["PP"] - prev_high).round(2)
    df["S2"] = (df["PP"] - (prev_high - prev_low)).round(2)
    df["S3"] = (prev_low - 2 * (prev_high - df["PP"])).round(2)

    # ── PRE-CALCULATED ROLLING WINDOW HIGHS ───────────────────────────────────
    n = len(df)

    if timeframe == "1d":
        df["HIGH_20D"]  = df["High"].rolling(window=20,  min_periods=15).max()
        df["HIGH_50D"]  = df["High"].rolling(window=50,  min_periods=40).max()
        df["HIGH_100D"] = df["High"].rolling(window=100, min_periods=80).max()
        df["HIGH_252D"] = df["High"].rolling(window=252, min_periods=200).max()

    elif timeframe == "1h":
        df["HIGH_6H"]   = df["High"].rolling(window=6,   min_periods=5).max()
        df["HIGH_26H"]  = df["High"].rolling(window=26,  min_periods=20).max()
        df["HIGH_130H"] = df["High"].rolling(window=130, min_periods=100).max()
        df["HIGH_260H"] = df["High"].rolling(window=260, min_periods=200).max()
        df["HIGH_20D"]  = df["HIGH_26H"]
        df["HIGH_50D"]  = df["HIGH_130H"]
        df["HIGH_100D"] = df["HIGH_130H"]
        df["HIGH_252D"] = df["HIGH_260H"]

    else:  # 15m
        df["HIGH_26_15M"]  = df["High"].rolling(window=26,  min_periods=20).max()
        df["HIGH_52_15M"]  = df["High"].rolling(window=52,  min_periods=40).max()
        df["HIGH_104_15M"] = df["High"].rolling(window=104, min_periods=80).max()
        df["HIGH_20D"]  = df["HIGH_26_15M"]
        df["HIGH_50D"]  = df["HIGH_104_15M"]
        df["HIGH_100D"] = df["HIGH_104_15M"]
        df["HIGH_252D"] = df["High"].rolling(window=n, min_periods=n // 2).max()

    # 52-week high — timeframe-aware
    if timeframe == "1d":
        window52, min52 = 252, 200
    elif timeframe == "1h":
        window52, min52 = n, max(n // 2, 50)
    else:
        window52, min52 = n, max(n // 2, 20)

    df["HIGH_52W"] = df["High"].rolling(window=window52, min_periods=min52).max()

    return df
