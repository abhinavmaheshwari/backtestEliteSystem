import logging
from typing import Tuple
from app.database import get_connection

logger = logging.getLogger(__name__)

BASE_CAPITAL = 500000.0
RISK_PERCENT = 0.02  # 2% of total equity per trade

def get_portfolio_state() -> dict:
    """
    Returns the exact current state of the Live Portfolio:
    - total_equity (Realized)
    - available_margin
    - deployed_margin
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            # 1. Total realized PnL
            cur.execute("SELECT COALESCE(SUM(pnl_rs), 0) FROM alerts WHERE status IN ('WIN', 'LOSS')")
            realized_pnl = float(cur.fetchone()[0] or 0.0)
            
            # 2. Total allocated capital in open trades
            cur.execute("SELECT COALESCE(SUM(capital_allocated), 0) FROM alerts WHERE status = 'OPEN'")
            deployed_margin = float(cur.fetchone()[0] or 0.0)
            
    total_equity = BASE_CAPITAL + realized_pnl
    available_margin = total_equity - deployed_margin
    
    return {
        "total_equity": total_equity,
        "available_margin": available_margin,
        "deployed_margin": deployed_margin,
    }

def calculate_trade_allocation(entry_price: float, stop_loss: float, score: int = 80) -> Tuple[float, int]:
    """
    Calculates how much capital to deploy based on Trade Score.
    Score > 90: 3% risk
    Score 80-90: 2% risk
    Score < 80: 1% risk
    Bounded by Available Margin.
    Returns (capital_allocated, shares_bought)
    """
    if entry_price <= 0 or stop_loss <= 0:
        return 0.0, 0
        
    state = get_portfolio_state()
    total_equity = state["total_equity"]
    available_margin = state["available_margin"]
    
    # Dynamic Risk Assessment
    if score >= 90:
        dynamic_risk_percent = 0.03  # High conviction
    elif score >= 80:
        dynamic_risk_percent = 0.02  # Normal conviction
    else:
        dynamic_risk_percent = 0.01  # Lower conviction
        
    risk_rupees = total_equity * dynamic_risk_percent
    risk_per_share = abs(entry_price - stop_loss)
    
    if risk_per_share <= 0:
        return 0.0, 0
        
    shares_to_buy = int(risk_rupees / risk_per_share)
    capital_required = shares_to_buy * entry_price
    
    # Check if we have enough available cash. If not, scale down.
    if capital_required > available_margin:
        shares_to_buy = int(available_margin / entry_price)
        capital_required = shares_to_buy * entry_price
        
    return float(capital_required), shares_to_buy
