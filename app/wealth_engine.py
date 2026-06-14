import os
import time
import logging
import threading
import pandas as pd
import yfinance as yf
from datetime import datetime

logger = logging.getLogger(__name__)

def fetch_nifty_6m_return() -> float:
    try:
        ticker = yf.Ticker("^NSEI")
        hist = ticker.history(period="6m")
        if hist.empty or len(hist) < 2: return 0.0
        start_price = hist['Close'].iloc[0]
        end_price = hist['Close'].iloc[-1]
        return ((end_price - start_price) / start_price) * 100.0
    except Exception as e:
        logger.error(f"Failed to fetch Nifty: {e}")
        return 0.0

def calculate_wealth_technicals(symbol: str, nifty_6m_ret: float) -> dict:
    yf_symbol = symbol + ".NS" if not symbol.endswith(".NS") else symbol
    try:
        ticker = yf.Ticker(yf_symbol)
        hist = ticker.history(period="1y")
        if hist.empty or len(hist) < 120: # At least 6 months
            return {"sma_200": None, "cmp": None, "rs_6m": None, "dist_52w_high": None}
            
        hist['sma_200'] = hist['Close'].rolling(window=200).mean()
        
        last_row = hist.iloc[-1]
        cmp = float(last_row['Close'])
        
        # 6-Month RS
        hist_6m = hist.tail(126) # Approx 6 months
        if len(hist_6m) > 0:
            start_6m = hist_6m['Close'].iloc[0]
            stock_6m_ret = ((cmp - start_6m) / start_6m) * 100.0
            rs_6m = stock_6m_ret - nifty_6m_ret
        else:
            rs_6m = 0.0
            
        # 52W High
        high_52w = float(hist['High'].max())
        dist_52w_high = ((high_52w - cmp) / high_52w) * 100.0 if high_52w > 0 else 0.0
        
        return {
            "sma_200": float(last_row['sma_200']) if not pd.isna(last_row['sma_200']) else None,
            "cmp": cmp,
            "rs_6m": rs_6m,
            "dist_52w_high": dist_52w_high
        }
    except Exception as e:
        logger.error(f"Failed to fetch technicals for {symbol}: {e}")
        return {"sma_200": None, "cmp": None, "rs_6m": None, "dist_52w_high": None}


def calculate_100_point_score(r) -> int:
    score = 0
    
    # Quality (30)
    roe = r.get("ROE %", 0) or 0
    roce = r.get("ROCE %", 0) or 0
    de = r.get("Debt/Equity", 0) or 0
    
    if roe >= 15: score += 10
    elif roe >= 10: score += 5
    if roce >= 20: score += 10
    elif roce >= 15: score += 5
    if de <= 0.1: score += 10
    elif de <= 0.5: score += 5
    
    # Growth (30)
    yoy_sales = r.get("YOY Revenue %", 0) or 0
    yoy_profit = r.get("YOY Profit %", 0) or 0
    
    if yoy_sales >= 20: score += 15
    elif yoy_sales >= 12: score += 10
    if yoy_profit >= 20: score += 15
    elif yoy_profit >= 15: score += 10
    
    # Momentum (20)
    rs_6m = r.get("rs_6m", 0) or 0
    dist_52w = r.get("dist_52w_high", 100) or 100
    cmp = r.get("cmp", 0) or 0
    sma_200 = r.get("sma_200", 0) or 0
    
    if rs_6m > 15: score += 10
    elif rs_6m > 0: score += 5
    if dist_52w <= 10: score += 5
    if cmp > sma_200 and sma_200 > 0: score += 5
    
    # Ownership & AI (20)
    cats = str(r.get("Category", ""))
    ai_boost = r.get("AI Confidence Boost", 0) or 0
    
    if "Inst Accumulation" in cats: score += 10
    if ai_boost >= 10: score += 10
    elif ai_boost >= 5: score += 5
    
    return min(100, score)


def determine_portfolio_bucket(r):
    score = r.get("FM_Score", 0)
    mcap = r.get("Market Cap Cr", 0) or 0
    roce = r.get("ROCE %", 0) or 0
    roe = r.get("ROE %", 0) or 0
    de = r.get("Debt/Equity", 0) or 0
    yoy_sales = r.get("YOY Revenue %", 0) or 0
    yoy_profit = r.get("YOY Profit %", 0) or 0
    rs_6m = r.get("rs_6m", 0) or 0
    dist_52w = r.get("dist_52w_high", 100) or 100
    cats = str(r.get("Category", ""))
    
    buckets = []
    
    # Core Compounder
    if score >= 80 and mcap >= 10000 and roce >= 20 and roe >= 15 and de <= 0.5:
        buckets.append("Core")
        
    # Growth Multiplier
    if score >= 75 and yoy_sales >= 20 and yoy_profit >= 20 and rs_6m > 0 and dist_52w <= 15:
        buckets.append("Growth")
        
    # Opportunistic / Turnaround
    if score >= 60 and ("Recovery Play" in cats or "Financial Recovery" in cats):
        buckets.append("Opportunistic")
        
    return ", ".join(buckets)


def run_wealth_loop():
    from config import WATCHLIST_PATH, DATA_DIR
    from database import upsert_scanner_health
    from telegram_utils import send_telegram_alert
    
    WEALTH_PATH = os.path.join(DATA_DIR, "elite_wealth_system.parquet")
    logger.info("💰 Fund Manager Wealth Engine Started.")
    upsert_scanner_health("Wealth Engine", "IDLE", last_success=None, today_alerts=0)
    
    last_telegram_week = -1
    
    while True:
        try:
            if not os.path.exists(WATCHLIST_PATH):
                time.sleep(300)
                continue
                
            df = pd.read_parquet(WATCHLIST_PATH)
            
            logger.info(f"💰 [WEALTH ENGINE] Calculating Fund Manager metrics for {len(df)} elite stocks...")
            
            nifty_6m_ret = fetch_nifty_6m_return()
            
            technicals = []
            for i, row in df.iterrows():
                sym = row["Stock"]
                tech = calculate_wealth_technicals(sym, nifty_6m_ret)
                tech["Stock"] = sym
                technicals.append(tech)
                time.sleep(0.5)
                
            tech_df = pd.DataFrame(technicals)
            wealth_df = pd.merge(df, tech_df, on="Stock", how="left")
            
            # Apply 100-point score
            wealth_df["FM_Score"] = wealth_df.apply(calculate_100_point_score, axis=1)
            wealth_df["Portfolio_Bucket"] = wealth_df.apply(determine_portfolio_bucket, axis=1)
            
            # Buy/Sell Signals
            def get_signal(r):
                score = r.get("FM_Score", 0)
                cmp = r.get("cmp", 0) or 0
                sma = r.get("sma_200", 0) or 0
                if score >= 85 and cmp > sma:
                    return f"BUY (Score: {score})"
                if score < 60:
                    return f"SELL (Score: {score})"
                return ""
                
            wealth_df["Signal"] = wealth_df.apply(get_signal, axis=1)
            
            wealth_df.to_parquet(WEALTH_PATH, index=False)
            
            logger.info("✅ [WEALTH ENGINE] Updated Fund Manager Parquet.")
            upsert_scanner_health("Wealth Engine", "OK", last_success=datetime.now().isoformat(), today_alerts=len(wealth_df[wealth_df["FM_Score"] >= 85]))
            
            # Weekly Telegram Alert (Run on Sunday)
            now = datetime.now()
            current_week = now.isocalendar()[1]
            if now.weekday() == 6 and current_week != last_telegram_week:
                # Get Top 20
                top_20 = wealth_df.sort_values(by="FM_Score", ascending=False).head(20)
                msg = "🏆 **Top 20 Long-Term Compounders** 🏆\n\n"
                for idx, row in top_20.iterrows():
                    msg += f"• **{row['Stock']}** | Score: {row['FM_Score']}\n"
                    msg += f"  └ ROCE: {row.get('ROCE %', 0):.1f}% | RS: {row.get('rs_6m', 0):.1f}%\n"
                
                send_telegram_alert(msg, parse_mode="Markdown")
                last_telegram_week = current_week
            
        except Exception as e:
            logger.error(f"❌ [WEALTH ENGINE] Loop crashed: {e}")
            upsert_scanner_health("Wealth Engine", "DOWN", error_msg=str(e))
            
        time.sleep(3600) # Run once an hour
