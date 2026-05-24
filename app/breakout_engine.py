import numpy as np


def detect_breakouts(df):

    latest = df.iloc[-1]

    breakout_signals = []

    # DAILY BREAKOUT
    previous_20d_high = df["High"].rolling(20).max().iloc[-2]

    if latest["Close"] > previous_20d_high:
        breakout_signals.append("Daily Breakout")

    # WEEKLY BREAKOUT
    previous_50d_high = df["High"].rolling(50).max().iloc[-2]

    if latest["Close"] > previous_50d_high:
        breakout_signals.append("Weekly Breakout")

    # MONTHLY BREAKOUT
    previous_100d_high = df["High"].rolling(100).max().iloc[-2]

    if latest["Close"] > previous_100d_high:
        breakout_signals.append("Monthly Breakout")

    # MULTIYEAR BREAKOUT
    previous_252d_high = df["High"].rolling(252).max().iloc[-2]

    if latest["Close"] > previous_252d_high:
        breakout_signals.append("52W Breakout")

    return breakout_signals
