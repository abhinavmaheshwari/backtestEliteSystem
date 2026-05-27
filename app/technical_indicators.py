# =====================================================================================
# app/technical_indicators.py
# =====================================================================================

import pandas as pd
import ta


def apply_indicators(df: pd.DataFrame, timeframe: str = "1d") -> pd.DataFrame:
    """
    Applies all technical indicators and returns the enriched DataFrame.

    Parameters
    ----------
    df        : OHLCV DataFrame
    timeframe : "15m" | "1h" | "1d"
                Used to compute HIGH_52W with an appropriate lookback window
                instead of a fixed 252-bar window that only makes sense for daily bars.

    Columns produced (all consumed by scoring_engine / breakout_engine):
        EMA20, SMA50, SMA200          — trend MAs
        RSI                           — momentum
        ATR                           — volatility (position sizing)
        BB_UPPER, BB_LOWER, BB_MID    — Bollinger Bands (disqualifier #6)
        ADX                           — directional strength (disqualifier #4 + trend pts)
        MACD, MACD_SIGNAL, MACD_HIST  — momentum confirmation (trend +2 pts)
        HIGH_52W                      — rolling 52-week high, timeframe-aware (bonus +2 pts)
    """

    if df is None or df.empty:
        return df

    close = df["Close"]
    high  = df["High"]
    low   = df["Low"]

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
    # BB_UPPER consumed by overextension disqualifier in scoring_engine
    # ------------------------------------------------------------------
    df["ATR"] = ta.volatility.average_true_range(high, low, close, window=14)

    bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)
    df["BB_UPPER"] = bb.bollinger_hband()
    df["BB_LOWER"] = bb.bollinger_lband()
    df["BB_MID"]   = bb.bollinger_mavg()

    # ------------------------------------------------------------------
    # TREND DIRECTION — ADX
    # Consumed by: disqualifier #4 (< 20 → reject) + trend scoring (≥ 25 → +2 pts)
    # ------------------------------------------------------------------
    adx_ind   = ta.trend.ADXIndicator(high, low, close, window=14)
    df["ADX"] = adx_ind.adx()

    # ------------------------------------------------------------------
    # MOMENTUM CONFIRMATION — MACD
    # Consumed by: trend scoring (+2 pts when MACD > MACD_SIGNAL)
    # ------------------------------------------------------------------
    macd_ind          = ta.trend.MACD(close, window_slow=26, window_fast=12, window_sign=9)
    df["MACD"]        = macd_ind.macd()
    df["MACD_SIGNAL"] = macd_ind.macd_signal()
    df["MACD_HIST"]   = macd_ind.macd_diff()

    # ------------------------------------------------------------------
    # 52-WEEK HIGH — timeframe-aware rolling window
    #
    # Daily  (1d)  : 252 bars ≈ 1 trading year   (min_periods=200)
    # Hourly (1h)  : 252 * 6.5h ≈ 1638 bars — too many for 60d of data
    #                → use full available history (whatever fits in 60d window)
    # 15-min (15m) : 252 * 26 bars ≈ 6552 bars — never available in 5d
    #                → use full available history (rolling max of all bars)
    #
    # Setting min_periods to len(df)//2 means HIGH_52W only populates once
    # enough data exists to be a meaningful reference.
    # ------------------------------------------------------------------
    n = len(df)
    if timeframe == "1d":
        window52      = 252
        min_periods52 = 200
    elif timeframe == "1h":
        # 60 days × ~26 bars/day ≈ 1560 bars; use all of it
        window52      = n
        min_periods52 = max(n // 2, 50)
    else:
        # 15m: 5 days × ~26 bars/day ≈ 130 bars; use all of it
        window52      = n
        min_periods52 = max(n // 2, 20)

    df["HIGH_52W"] = df["High"].rolling(window=window52, min_periods=min_periods52).max()

    return df
