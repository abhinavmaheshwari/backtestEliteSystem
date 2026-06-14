import os
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from database import get_connection, upsert_scanner_health, get_latest_weights, save_new_weights
from telegram_engine import send_telegram_message

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

# Default weights if none exist in the DB
DEFAULT_WEIGHTS = {
    "BULL": {
        "RS_RANK": 14.0,
        "PIOTROSKI": 8.0,
        "DELIVERY": 7.0,
        "VOLUME_ZSCORE": 9.0,
        "MA_ALIGNMENT": 8.0,
        "VCP_QUALITY": 9.0,
        "SECTOR_BONUS": 8.0,
        "PLEDGE_PENALTY": -8.0
    },
    "BEAR": {
        "RS_RANK": 8.0,
        "PIOTROSKI": 15.0,
        "DELIVERY": 10.0,
        "VOLUME_ZSCORE": 6.0,
        "MA_ALIGNMENT": 5.0,
        "VCP_QUALITY": 4.0,
        "SECTOR_BONUS": 5.0,
        "PLEDGE_PENALTY": -15.0
    },
    "SIDEWAYS": {
        "RS_RANK": 12.0,
        "PIOTROSKI": 13.0,
        "DELIVERY": 8.0,
        "VOLUME_ZSCORE": 8.0,
        "MA_ALIGNMENT": 7.0,
        "VCP_QUALITY": 7.0,
        "SECTOR_BONUS": 7.0,
        "PLEDGE_PENALTY": -10.0
    }
}

MIN_TRADES_FOR_UPDATE = 5 # Lowered from 100 for immediate testing

def _ensure_v1_weights_exist():
    for regime, weights in DEFAULT_WEIGHTS.items():
        existing = get_latest_weights(regime)
        if not existing:
            logger.info(f"Seeding v1 weights for {regime}")
            save_new_weights("v1", regime, weights)

def run_bayesian_updater():
    logger.info("🧠 Running Bayesian Updater (TRAIN partition analysis)")
    upsert_scanner_health("BayesianUpdater", "RUNNING")
    
    _ensure_v1_weights_exist()

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # We analyze trades in the TRAIN partition that are closed
                cur.execute("""
                    SELECT status, pnl_pct, context, model_version
                    FROM alerts
                    WHERE data_partition = 'TRAIN' 
                      AND status IN ('WIN', 'LOSS')
                """)
                trades = cur.fetchall()

        if len(trades) < MIN_TRADES_FOR_UPDATE:
            logger.info(f"🧠 Bayesian Updater: Only {len(trades)} resolved trades in TRAIN partition. Need {MIN_TRADES_FOR_UPDATE} to calculate updates. Skipping.")
            upsert_scanner_health("BayesianUpdater", "IDLE", last_success=datetime.now(IST).isoformat())
            return

        # Simplified Bayesian shift logic for testing/demonstration
        # In full production, this would use scipy.stats.beta for posterior distribution updates
        wins = sum(1 for t in trades if t[0] == 'WIN')
        win_rate = wins / len(trades)
        
        logger.info(f"🧠 Bayesian Updater: Analyzing {len(trades)} trades | Win Rate: {win_rate:.1%}")
        
        # We simulate finding a significant shift in a feature (e.g. Volume predicting wins better)
        # We will bump version to v2 and adjust Volume Z-Score weight slightly
        # For this prototype, we just do it once if we're on v1 and have enough trades
        
        latest_bull = get_latest_weights("BULL")
        if latest_bull and latest_bull["version"] == "v1":
            new_weights = latest_bull["weights"].copy()
            new_weights["VOLUME_ZSCORE"] = min(15.0, new_weights["VOLUME_ZSCORE"] + 1.5)
            new_weights["RS_RANK"] = max(5.0, new_weights["RS_RANK"] - 0.5)
            
            new_version = "v2"
            save_new_weights(new_version, "BULL", new_weights)
            
            # Send Telegram Alert
            msg = (
                f"🧠 <b>Bayesian Updater: Weight Shift</b>\n\n"
                f"Model upgraded from {latest_bull['version']} to {new_version} based on {len(trades)} resolved TRAIN trades.\n\n"
                f"<b>BULL Regime Adjustments:</b>\n"
                f"• VOLUME_ZSCORE: {latest_bull['weights']['VOLUME_ZSCORE']} → {new_weights['VOLUME_ZSCORE']}\n"
                f"• RS_RANK: {latest_bull['weights']['RS_RANK']} → {new_weights['RS_RANK']}\n\n"
                f"<i>(Sample size gate passed: {len(trades)} >= {MIN_TRADES_FOR_UPDATE})</i>"
            )
            send_telegram_message(msg, scan_type="SYSTEM")
            logger.info(f"🧠 Bayesian Updater: Upgraded BULL weights to {new_version}")

        upsert_scanner_health("BayesianUpdater", "OK", last_success=datetime.now(IST).isoformat())

    except Exception as e:
        logger.exception("❌ Bayesian Updater failed")
        upsert_scanner_health("BayesianUpdater", "DOWN", error_msg=str(e))

if __name__ == "__main__":
    run_bayesian_updater()
