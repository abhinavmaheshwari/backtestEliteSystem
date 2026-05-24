import pandas as pd
import ta


def apply_indicators(df):

    df["EMA20"] = ta.trend.ema_indicator(df["Close"], window=20)

    df["SMA50"] = ta.trend.sma_indicator(df["Close"], window=50)

    df["SMA200"] = ta.trend.sma_indicator(df["Close"], window=200)

    df["RSI"] = ta.momentum.rsi(df["Close"], window=14)

    df["ATR"] = ta.volatility.average_true_range(
        df["High"],
        df["Low"],
        df["Close"]
    )

    return df
