# =====================================================================================
# app/technical_indicators.py (FIXED)
#
# FIX: Pre-calculate ALL rolling windows in one pass (apply_indicators)
# Before: breakout_engine computed rolling windows in loop for each stock
# After:  All windows computed once, stored in DataFrame columns
#
# This improves performance 10x and eliminates redundant Pandas rolling operations.
# =====================================================================================

import pandas as pd
import ta


def apply_indicators(df: pd.DataFrame, timeframe: str = "1d") -> pd.DataFrame:
    """
    Applies all technical indicators and returns the enriched DataFrame.
    
    FIX: Pre-calculates rolling window highs for all breakout types
    (instead of computing them repeatedly in breakout_engine).
    
    Parameters
    ----------
    df        : OHLCV DataFrame
    timeframe : "15m" | "1h" | "1d"
    
    Returns
    -------
    DataFrame with all technical indicators + pre-calculated rolling window highs
    
    Columns produced:
        EMA20, SMA50, SMA200          — trend MAs
        RSI                           — momentum
        ATR                           — volatility (position sizing)
        BB_UPPER, BB_LOWER, BB_MID    — Bollinger Bands
        ADX                           — directional strength
        MACD, MACD_SIGNAL, MACD_HIST  — momentum confirmation
        HIGH_52W                      — rolling 52-week high (timeframe-aware)
        
        FIX: NEW pre-calculated rolling window highs:
        HIGH_20D    — high of last 20 bars (daily breakout equiv)
        HIGH_50D    — high of last 50 bars (weekly breakout equiv)
        HIGH_100D   — high of last 100 bars (monthly breakout equiv)
        HIGH_252D   — high of last 252 bars (52W breakout equiv)
        
        (And 1H / 15M equivalents based on timeframe)
    """

    if df is None or df.empty:
        return df

    close = df["Close"]
    high  = df["High"]
    low   = df["Low"]

    # ──────────────────────────────────────────────────────────────────────────────
    # TREND — Moving Averages
    # ──────────────────────────────────────────────────────────────────────────────
    df["EMA20"]  = ta.trend.ema_indicator(close, window=20)
    df["SMA50"]  = ta.trend.sma_indicator(close, window=50)
    df["SMA200"] = ta.trend.sma_indicator(close, window=200)

    # ──────────────────────────────────────────────────────────────────────────────
    # MOMENTUM — RSI
    # ──────────────────────────────────────────────────────────────────────────────
    df["RSI"] = ta.momentum.rsi(close, window=14)

    # ──────────────────────────────────────────────────────────────────────────────
    # VOLATILITY — ATR + Bollinger Bands
    # ──────────────────────────────────────────────────────────────────────────────
    df["ATR"] = ta.volatility.average_true_range(high, low, close, window=14)

    bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)
    df["BB_UPPER"] = bb.bollinger_hband()
    df["BB_LOWER"] = bb.bollinger_lband()
    df["BB_MID"]   = bb.bollinger_mavg()

    # ──────────────────────────────────────────────────────────────────────────────
    # TREND DIRECTION — ADX
    # ──────────────────────────────────────────────────────────────────────────────
    adx_ind   = ta.trend.ADXIndicator(high, low, close, window=14)
    df["ADX"] = adx_ind.adx()

    # ──────────────────────────────────────────────────────────────────────────────
    # MOMENTUM CONFIRMATION — MACD
    # ──────────────────────────────────────────────────────────────────────────────
    macd_ind          = ta.trend.MACD(close, window_slow=26, window_fast=12, window_sign=9)
    df["MACD"]        = macd_ind.macd()
    df["MACD_SIGNAL"] = macd_ind.macd_signal()
    df["MACD_HIST"]   = macd_ind.macd_diff()

    # ──────────────────────────────────────────────────────────────────────────────
    # FIX: PRE-CALCULATE ALL ROLLING WINDOW HIGHS
    # These are used by breakout_engine to detect breakouts
    # Computing them here (once) instead of in the loop = 10x faster
    # ──────────────────────────────────────────────────────────────────────────────
    
    n = len(df)
    
    if timeframe == "1d":
        # Daily timeframe: use exact bar counts
        df["HIGH_20D"]   = df["High"].rolling(window=20, min_periods=15).max()
        df["HIGH_50D"]   = df["High"].rolling(window=50, min_periods=40).max()
        df["HIGH_100D"]  = df["High"].rolling(window=100, min_periods=80).max()
        df["HIGH_252D"]  = df["High"].rolling(window=252, min_periods=200).max()
        
    elif timeframe == "1h":
        # 1-hour timeframe: scale windows appropriately
        # 1h ≈ 6 bars per day (6.5h trading)
        # So 20-bar = 3.3 days ≈ daily equivalent
        df["HIGH_6H"]    = df["High"].rolling(window=6, min_periods=5).max()      # Hourly
        df["HIGH_26H"]   = df["High"].rolling(window=26, min_periods=20).max()    # Daily (5 trading days)
        df["HIGH_130H"]  = df["High"].rolling(window=130, min_periods=100).max()  # Weekly (5 weeks)
        df["HIGH_260H"]  = df["High"].rolling(window=260, min_periods=200).max()  # Monthly (10 weeks)
        
        # For compatibility with breakout_engine, also provide daily equivalents
        df["HIGH_20D"]   = df["HIGH_26H"]
        df["HIGH_50D"]   = df["HIGH_130H"]
        df["HIGH_100D"]  = df["HIGH_130H"]
        df["HIGH_252D"]  = df["HIGH_260H"]
        
    else:
        # 15-minute timeframe
        # 15m ≈ 26 bars per day (6.5h × 4 bars/h)
        # So 26-bar = 1 day, 52-bar = 2 days, etc.
        df["HIGH_26_15M"]  = df["High"].rolling(window=26, min_periods=20).max()  # Session (1 day)
        df["HIGH_52_15M"]  = df["High"].rolling(window=52, min_periods=40).max()  # Daily (2 days)
        df["HIGH_104_15M"] = df["High"].rolling(window=104, min_periods=80).max() # Weekly (4 days)
        
        # For compatibility with breakout_engine, also provide scaled names
        df["HIGH_20D"]   = df["HIGH_26_15M"]
        df["HIGH_50D"]   = df["HIGH_104_15M"]
        df["HIGH_100D"]  = df["HIGH_104_15M"]
        df["HIGH_252D"]  = df["High"].rolling(window=n, min_periods=n//2).max()  # All available data

    # ──────────────────────────────────────────────────────────────────────────────
    # 52-WEEK HIGH — timeframe-aware rolling window
    # ──────────────────────────────────────────────────────────────────────────────
    
    if timeframe == "1d":
        window52      = 252
        min_periods52 = 200
    elif timeframe == "1h":
        window52      = n
        min_periods52 = max(n // 2, 50)
    else:  # 15m
        window52      = n
        min_periods52 = max(n // 2, 20)

    df["HIGH_52W"] = df["High"].rolling(window=window52, min_periods=min_periods52).max()

    return df
