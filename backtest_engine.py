import os
os.environ["BACKTEST_MODE"] = "true"

import time
from freezegun import freeze_time
from datetime import datetime, timedelta
import pandas_market_calendars as mcal
import logging
from zoneinfo import ZoneInfo

# Import your existing modules AFTER env is set
# Import your existing modules AFTER env is set
from app import daily_builder, wealth_engine, intraday, live_scanner
from app import performance_tracker, eod_scanner, reversal_scanner

IST = ZoneInfo("Asia/Kolkata")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

NSE_CAL = mcal.get_calendar("NSE")
START   = datetime(2026, 1, 1)
try:
    from database import get_system_state
    sim_date_str = get_system_state("simulated_date")
    if sim_date_str:
        START = datetime.strptime(sim_date_str, "%Y-%m-%d")
except Exception:
    pass

END     = datetime(2026, 6, 19)

def get_trading_days(start, end):
    schedule = NSE_CAL.schedule(start_date=start, end_date=end)
    return mcal.date_range(schedule, frequency="1D")

def simulate_day(sim_date: datetime):
    date_str = sim_date.strftime("%Y-%m-%d")
    logger.info("=" * 80)
    logger.info(f"🚀 SIMULATING DAY: {date_str}")
    logger.info("=" * 80)
    
    try:
        from database import save_system_state
        save_system_state("simulated_date", date_str)
    except Exception as e:
        logger.error(f"Failed to save simulated_date: {e}")
    
    # --- Pre-market: 1:00 AM ---
    t_premarket = datetime.strptime(f"{date_str} 01:00:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
    with freeze_time(t_premarket):
        logger.info(f"[{date_str} 01:00:00] Running daily_builder")
        try:
            daily_builder.build_watchlist()
        except Exception as e:
            logger.error(f"daily_builder failed: {e}")
    
    # --- Wealth scan: 1:05 AM ---
    t_wealth = datetime.strptime(f"{date_str} 01:05:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
    with freeze_time(t_wealth):
        logger.info(f"[{date_str} 01:05:00] Running wealth_engine")
        try:
            wealth_engine.run_wealth_scan()
        except Exception as e:
            logger.error(f"wealth_engine failed: {e}")
    
    # --- Intraday loop: 9:15 to 15:30 in 5-min ticks ---
    # OPTIMIZATION: Only run intraday if date is >= 2026-04-20 (since we only have 60 days of 15m data)
    if sim_date >= datetime(2026, 4, 20):
        tick = datetime.strptime(f"{date_str} 09:15:00", "%Y-%m-%d %H:%M:%S")
        market_close = datetime.strptime(f"{date_str} 15:30:00", "%Y-%m-%d %H:%M:%S")
        
        while tick <= market_close:
            tick_str = tick.strftime("%Y-%m-%d %H:%M:%S")
            t_tick = tick.replace(tzinfo=IST)
            with freeze_time(t_tick):
                logger.info(f"[{tick_str}] Intraday tick")
                try:
                    intraday.start(run_once=True)
                except Exception as e:
                    logger.error(f"intraday failed: {e}")
                    
                try:
                    live_scanner.start(run_once=True)
                except Exception as e:
                    logger.error(f"live_scanner failed: {e}")
                    
            tick += timedelta(minutes=15)  # Our intraday runs on 15m intervals in price_fetcher actually
    else:
        logger.info(f"[{date_str}] Skipping Intraday logic (No 15m data exists before April 20th)")
    
    # --- EOD processing ---
    t_perf = datetime.strptime(f"{date_str} 15:35:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
    with freeze_time(t_perf):
        logger.info(f"[{date_str} 15:35:00] Running performance_tracker")
        try:
            performance_tracker.build_performance_data()
        except Exception as e:
            logger.error(f"performance_tracker failed: {e}")
    
    t_eod = datetime.strptime(f"{date_str} 18:30:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
    with freeze_time(t_eod):
        logger.info(f"[{date_str} 18:30:00] Running EOD and Reversal scanners")
        try:
            eod_scanner.start()
        except Exception as e:
            logger.error(f"eod_scanner failed: {e}")
            
        try:
            reversal_scanner.start()
        except Exception as e:
            logger.error(f"reversal_scanner failed: {e}")

def run_backtest():
    trading_days = get_trading_days(START, END)
    logger.info(f"Starting backtest: {len(trading_days)} trading days from {START.date()} to {END.date()}")
    
    for day in trading_days:
        # Use timezone naive datetime matching the simulated date
        sim_date = day.to_pydatetime()
        simulate_day(sim_date)
        
    logger.info("✅ BACKTEST COMPLETE")

if __name__ == "__main__":
    run_backtest()
