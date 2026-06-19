import os
os.environ["BACKTEST_MODE"] = "true"

import time
from freezegun import freeze_time
from datetime import datetime, timedelta
import pandas_market_calendars as mcal
import logging
from zoneinfo import ZoneInfo

# Import your existing modules AFTER env is set
from app import daily_builder, wealth_engine, intraday, live_scanner
from app import performance_tracker, eod_scanner, reversal_scanner

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

NSE_CAL = mcal.get_calendar("NSE")
START   = datetime(2026, 1, 1)
END     = datetime(2026, 6, 19)

def get_trading_days(start, end):
    schedule = NSE_CAL.schedule(start_date=start, end_date=end)
    return mcal.date_range(schedule, frequency="1D")

def simulate_day(sim_date: datetime):
    date_str = sim_date.strftime("%Y-%m-%d")
    logger.info("=" * 80)
    logger.info(f"🚀 SIMULATING DAY: {date_str}")
    logger.info("=" * 80)
    
    # --- Pre-market: 1:00 AM ---
    with freeze_time(f"{date_str} 01:00:00", tz_offset=0):
        # Timezone for freezegun can be tricky; we'll assume it mocks local time 
        # which our app sets to IST via ZoneInfo
        logger.info(f"[{date_str} 01:00:00] Running daily_builder")
        try:
            daily_builder.build_watchlist()
        except Exception as e:
            logger.error(f"daily_builder failed: {e}")
    
    # --- Wealth scan: 1:05 AM ---
    with freeze_time(f"{date_str} 01:05:00", tz_offset=0):
        logger.info(f"[{date_str} 01:05:00] Running wealth_engine")
        try:
            wealth_engine.run_wealth_scan()
        except Exception as e:
            logger.error(f"wealth_engine failed: {e}")
    
    # --- Intraday loop: 9:15 to 15:30 in 5-min ticks ---
    tick = datetime.strptime(f"{date_str} 09:15:00", "%Y-%m-%d %H:%M:%S")
    market_close = datetime.strptime(f"{date_str} 15:30:00", "%Y-%m-%d %H:%M:%S")
    
    while tick <= market_close:
        tick_str = tick.strftime("%Y-%m-%d %H:%M:%S")
        with freeze_time(tick_str, tz_offset=0):
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
    
    # --- EOD processing ---
    with freeze_time(f"{date_str} 15:35:00", tz_offset=0):
        logger.info(f"[{date_str} 15:35:00] Running performance_tracker")
        try:
            performance_tracker.build_performance_data()
        except Exception as e:
            logger.error(f"performance_tracker failed: {e}")
    
    with freeze_time(f"{date_str} 18:30:00", tz_offset=0):
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
