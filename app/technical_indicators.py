# =====================================================================================
# app/technical_indicators.py
# =====================================================================================
#
# Columns produced (all consumed by scoring_engine.py / breakout_engine.py):
#
#   Trend MAs   : EMA20, SMA50, SMA200
#   Momentum    : RSI
#   Volatility  : ATR, BB_UPPER  (Bollinger upper band — disqualifier #6)
#   Trend dir.  : ADX            (disqualifier #4 + trend scoring)
#   Momentum    : MACD, MACD_SIGNAL (trend scoring +2 pts)
#   Range ref.  : HIGH_52W       (bonus modifier — near-ATH check)
#
# =====================================================================================

import pandas as pd
import ta


def apply_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Applies all technical indicators in-place and returns the enriched DataFrame.
    Returns None if the DataFrame is empty or too short to be useful.
    """

    if df is None or df.empty:
        return df

    close  = df["Close"]
    high   = df["High"]
    low    = df["Low"]

    # ------------------------------------------------------------------
    # TREND — Moving Averages
    # ------------------------------------------------------------------
    df["EMA20"]  = ta.trend.ema_indicator(close, window=20)
    df["SMA50"]  = ta.trend.sma_indicator(close, window=50)
    df["SMA200"] = ta.trend.sma_indicator(close, window=200)

    # ------------------------------------------------------------------
    # MOMENTUM — RSI
    # ------------------------------------------------------------------
    df["RSI"] = ta.momentum.rsi(close, window=14)

    # ------------------------------------------------------------------
    # VOLATILITY — ATR + Bollinger Bands
    # BB_UPPER is used by the overextension disqualifier in scoring_engine
    # ------------------------------------------------------------------
    df["ATR"] = ta.volatility.average_true_range(high, low, close, window=14)

    bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)
    df["BB_UPPER"] = bb.bollinger_hband()
    df["BB_LOWER"] = bb.bollinger_lband()   # kept for future use
    df["BB_MID"]   = bb.bollinger_mavg()    # kept for future use

    # ------------------------------------------------------------------
    # TREND DIRECTION — ADX
    # Used by: disqualifier #4 (ADX < 20 → reject) and trend scoring (+1/+2 pts)
    # ------------------------------------------------------------------
    adx_ind    = ta.trend.ADXIndicator(high, low, close, window=14)
    df["ADX"]  = adx_ind.adx()

    # ------------------------------------------------------------------
    # MOMENTUM CONFIRMATION — MACD
    # Used by: trend scoring (+2 pts when MACD > MACD_SIGNAL)
    # ------------------------------------------------------------------
    macd_ind        = ta.trend.MACD(close, window_slow=26, window_fast=12, window_sign=9)
    df["MACD"]        = macd_ind.macd()
    df["MACD_SIGNAL"] = macd_ind.macd_signal()
    df["MACD_HIST"]   = macd_ind.macd_diff()   # kept for future use

    # ------------------------------------------------------------------
    # 52-WEEK HIGH — rolling 252-bar high
    # Used by: bonus_modifiers (+2 pts when close within 3% of HIGH_52W)
    # Also used internally by breakout_engine for the 52W Breakout signal
    # ------------------------------------------------------------------
    df["HIGH_52W"] = df["High"].rolling(window=252, min_periods=200).max()

    return df
