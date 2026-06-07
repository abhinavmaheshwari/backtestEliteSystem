# =====================================================================================
# app/market_filter.py
# MACRO REGIME GATEKEEPER
# =====================================================================================
import yfinance as yf
import pandas as pd
import logging

logger = logging.getLogger(__name__)

def is_market_regime_bullish() -> bool:
    """
    Checks if the broader Nifty 50 index is in an established short-term uptrend.
    
    HOW THIS IMPROVES RESULTS:
    Momentum strategies require a rising tide. Individual breakouts will fail and 
    hit stop-losses constantly if the macro index is pulling back. This function
    acts as a master switch: no bull market, no breakout alerts.
    """
    try:
        # Fetch Nifty 50 daily data
        nifty = yf.download("^NSEI", period="50d", interval="1d", progress=False)
        if nifty is None or nifty.empty:
            return True  # Fail-safe: allow scans if Yahoo API drops the Nifty data
            
        # Handle multi-index columns if yfinance returns them
        if isinstance(nifty.columns, pd.MultiIndex):
            nifty.columns = nifty.columns.get_level_values(0)
            
        # Calculate 20-day Exponential Moving Average
        nifty["EMA20"] = nifty["Close"].ewm(span=20, adjust=False).mean()
        
        latest_close = float(nifty["Close"].iloc[-1])
        latest_ema20 = float(nifty["EMA20"].iloc[-1])
        
        # Regime is bullish ONLY if the index closes above its 20 EMA
        is_bullish = latest_close > latest_ema20
        
        if not is_bullish:
            logger.info(f"🛑 Nifty 50 (₹{latest_close:.2f}) is below EMA20 (₹{latest_ema20:.2f}). Regime is BEARISH.")
            
        return is_bullish
        
    except Exception as e:
        logger.warning(f"⚠️ Market regime check failed: {e}. Defaulting to True.")
        return True
