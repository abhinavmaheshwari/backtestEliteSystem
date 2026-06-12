# =====================================================================================
# app/technical_indicators.py  (UPGRADED v2)
#
# CHANGES FROM v1:
#   1. Added SWING_LOW / SWING_HIGH detection  → used by SL/Target engine
#   2. Added Classic Pivot Points (PP, S1, S2, S3, R1, R2, R3)  → S/R levels
#   3. Added ATR_PCT (ATR as % of close) → volatility regime classifier
#   4. All pre-calculated rolling window highs retained (no regression)
# =====================================================================================

import pandas as pd
import ta


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
    -- Support / Resistance (NEW) --------------------------------------
    SWING_LOW    — rolling swing low  (support)
    SWING_HIGH   — rolling swing high (resistance)
    PP           — Classic Pivot Point
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
    df["ATR_PCT"] = (df["ATR"] / close * 100).round(2)   # NEW: volatility regime

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

    # ── SUPPORT / RESISTANCE — Swing Highs & Lows (NEW) ──────────────────────
    swing_window = {"1d": 20, "1h": 14, "15m": 10}.get(timeframe, 20)
    df["SWING_LOW"]  = low.rolling(window=swing_window,  min_periods=swing_window // 2).min()
    df["SWING_HIGH"] = high.rolling(window=swing_window, min_periods=swing_window // 2).max()

    # ── SUPPORT / RESISTANCE — Classic Pivot Points (NEW) ────────────────────
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

    # ── PRE-CALCULATED ROLLING WINDOW HIGHS (retained from v1) ───────────────
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
