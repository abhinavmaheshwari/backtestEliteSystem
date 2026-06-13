import os
import sys

# Ensure app path is loaded
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import get_connection
from app.portfolio_engine import BASE_CAPITAL

def run_migration():
    print("Starting Portfolio Migration...")
    
    with get_connection() as conn:
        with conn.cursor() as cur:
            # First, zero everything out in case of rerun
            cur.execute("UPDATE alerts SET capital_allocated = 0, shares_bought = 0, pnl_rs = 0")
            
            # Fetch all trades chronologically by alert_time
            cur.execute("""
                SELECT id, entry_price, stop_loss, exit_price, status, score, alert_time 
                FROM alerts 
                WHERE entry_price IS NOT NULL AND stop_loss IS NOT NULL
                ORDER BY alert_time ASC
            """)
            trades = cur.fetchall()
            
            for t in trades:
                tid = t[0]
                ep = float(t[1])
                sl = float(t[2])
                ex_p = float(t[3]) if t[3] is not None else None
                status = t[4]
                score = t[5] or 80
                alert_time = t[6]
                
                if ep <= 0 or sl <= 0:
                    continue
                
                # Realized PnL strictly before this trade started
                cur.execute("""
                    SELECT COALESCE(SUM(pnl_rs), 0) FROM alerts 
                    WHERE status IN ('WIN', 'LOSS') AND closed_at < %s
                """, (alert_time,))
                res = cur.fetchone()
                realized_pnl = float(res[0]) if res else 0.0
                
                # Capital locked up in trades that were OPEN at the time
                cur.execute("""
                    SELECT COALESCE(SUM(capital_allocated), 0) FROM alerts 
                    WHERE alert_time < %s AND (status = 'OPEN' OR (closed_at IS NOT NULL AND closed_at > %s))
                """, (alert_time, alert_time))
                res = cur.fetchone()
                deployed_margin = float(res[0]) if res else 0.0
                
                current_total_equity = BASE_CAPITAL + realized_pnl
                current_available = current_total_equity - deployed_margin
                
                # Dynamic Risk Assessment
                if score >= 90:
                    dynamic_risk_percent = 0.03
                elif score >= 80:
                    dynamic_risk_percent = 0.02
                else:
                    dynamic_risk_percent = 0.01
                    
                risk_rupees = current_total_equity * dynamic_risk_percent
                risk_per_share = abs(ep - sl)
                
                if risk_per_share <= 0:
                    continue
                    
                shares_to_buy = int(risk_rupees / risk_per_share)
                capital_required = shares_to_buy * ep
                
                if capital_required > current_available:
                    shares_to_buy = int(max(0, current_available) / ep)
                    capital_required = shares_to_buy * ep
                
                pnl_rs = 0.0
                if status in ('WIN', 'LOSS') and ex_p is not None:
                    pnl_rs = shares_to_buy * (ex_p - ep)
                    
                cur.execute("""
                    UPDATE alerts 
                    SET capital_allocated = %s, shares_bought = %s, pnl_rs = %s
                    WHERE id = %s
                """, (float(capital_required), shares_to_buy, float(pnl_rs), tid))
                
            conn.commit()
    print("Migration Complete!")

if __name__ == "__main__":
    run_migration()
