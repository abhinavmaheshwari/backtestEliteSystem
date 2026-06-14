import os
import time
import logging
import threading
import pandas as pd
import yfinance as yf
from datetime import datetime

logger = logging.getLogger(__name__)

def calculate_wealth_technicals(symbol: str) -> dict:
    """Fetches 200 SMA and RSI for a stock."""
    yf_symbol = symbol + ".NS" if not symbol.endswith(".NS") else symbol
    try:
        ticker = yf.Ticker(yf_symbol)
        hist = ticker.history(period="1y")
        if hist.empty or len(hist) < 200:
            return {"sma_200": None, "rsi": None, "cmp": None}
            
        hist['sma_200'] = hist['Close'].rolling(window=200).mean()
        
        # Calculate 14-day RSI
        delta = hist['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        hist['rsi'] = 100 - (100 / (1 + rs))
        
        last_row = hist.iloc[-1]
        
        return {
            "sma_200": float(last_row['sma_200']) if not pd.isna(last_row['sma_200']) else None,
            "rsi": float(last_row['rsi']) if not pd.isna(last_row['rsi']) else None,
            "cmp": float(last_row['Close'])
        }
    except Exception as e:
        logger.error(f"Failed to fetch technicals for {symbol}: {e}")
        return {"sma_200": None, "rsi": None, "cmp": None}


def run_wealth_loop():
    from config import WATCHLIST_PATH, DATA_DIR
    from database import upsert_scanner_health
    
    WEALTH_PATH = os.path.join(DATA_DIR, "elite_wealth_system.parquet")
    logger.info("💰 Wealth Engine Thread Started. Monitoring watchlist...")
    upsert_scanner_health("Wealth Engine", "IDLE", last_success=None, today_alerts=0)
    
    while True:
        try:
            if not os.path.exists(WATCHLIST_PATH):
                time.sleep(300)
                continue
                
            df = pd.read_parquet(WATCHLIST_PATH)
            
            logger.info(f"💰 [WEALTH ENGINE] Calculating long-term metrics for {len(df)} elite stocks...")
            
            technicals = []
            for i, row in df.iterrows():
                sym = row["Stock"]
                tech = calculate_wealth_technicals(sym)
                tech["Stock"] = sym
                technicals.append(tech)
                time.sleep(0.5) # Pace yfinance
                
            tech_df = pd.DataFrame(technicals)
            
            # Merge
            wealth_df = pd.merge(df, tech_df, on="Stock", how="left")
            
            # SIP Signal Logic
            # Strong Accumulate if CMP < 200 SMA OR RSI < 40
            def get_sip_signal(r):
                if pd.isna(r.get("cmp")) or pd.isna(r.get("sma_200")):
                    return ""
                
                cmp = r["cmp"]
                sma = r["sma_200"]
                rsi = r.get("rsi")
                
                signals = []
                if cmp < sma:
                    dist = ((sma - cmp) / sma) * 100
                    signals.append(f"Below 200 SMA (-{dist:.1f}%)")
                if rsi is not None and rsi < 40:
                    signals.append(f"Oversold (RSI: {rsi:.1f})")
                    
                if signals:
                    return " + ".join(signals)
                return ""
                
            wealth_df["SIP Signal"] = wealth_df.apply(get_sip_signal, axis=1)
            
            wealth_df.to_parquet(WEALTH_PATH, index=False)
            
            logger.info("✅ [WEALTH ENGINE] Updated Wealth System Parquet.")
            upsert_scanner_health("Wealth Engine", "OK", last_success=datetime.now().isoformat(), today_alerts=len(wealth_df[wealth_df["SIP Signal"] != ""]))
            
        except Exception as e:
            logger.error(f"❌ [WEALTH ENGINE] Loop crashed: {e}")
            upsert_scanner_health("Wealth Engine", "DOWN", error_msg=str(e))
            
        time.sleep(3600) # Run once an hour
