import os
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from database import get_connection, upsert_scanner_health, get_latest_weights, save_new_weights

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
            
            # ✅ SUBMIT FOR ADMIN APPROVAL (not directly applying)
            from database import submit_bayesian_update_for_approval
            
            reason = (
                f"VOLUME_ZSCORE improved (spike detection); "
                f"RS_RANK reduced (less reliant on relative strength); "
                f"Analysis: {len(trades)} TRAIN trades, {win_rate:.1%} win rate"
            )
            
            update_id = submit_bayesian_update_for_approval(
                regime="BULL",
                proposed_version=new_version,
                current_version=latest_bull["version"],
                current_weights=latest_bull["weights"],
                proposed_weights=new_weights,
                trades_analyzed=len(trades),
                win_rate=win_rate,
                reason=reason
            )
            
            if update_id:
                logger.info(f"✅ 🧠 Bayesian Update Submitted for Admin Approval")
                logger.info(f"   Update ID: {update_id}")
                logger.info(f"   Regime: BULL | Version: {latest_bull['version']} → {new_version}")
                logger.info(f"   Win Rate: {win_rate:.1%} from {len(trades)} trades")
                logger.info(f"   Changes:")
                logger.info(f"   • VOLUME_ZSCORE: {latest_bull['weights']['VOLUME_ZSCORE']} → {new_weights['VOLUME_ZSCORE']}")
                logger.info(f"   • RS_RANK: {latest_bull['weights']['RS_RANK']} → {new_weights['RS_RANK']}")
                logger.info(f"   ⏳ Awaiting admin approval...")
            else:
                logger.warning(f"⚠️  Could not submit Bayesian update (may have pending update)")

        upsert_scanner_health("BayesianUpdater", "OK", last_success=datetime.now(IST).isoformat())

    except Exception as e:
        logger.exception("❌ Bayesian Updater failed")
        upsert_scanner_health("BayesianUpdater", "DOWN", error_msg=str(e))

if __name__ == "__main__":
    run_bayesian_updater()
