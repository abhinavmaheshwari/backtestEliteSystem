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
    
    # Fixed Allocation Assessment based on Conviction (Score)
    if score >= 90:
        allocation_percent = 0.15  # High conviction: 15% of total equity
    else:
        allocation_percent = 0.10  # Normal/Low conviction: 10% of total equity
        
    capital_required = total_equity * allocation_percent
    
    # Check if we have enough available cash. If not, allocate whatever is left.
    if capital_required > available_margin:
        capital_required = max(0.0, available_margin)
        
    shares_to_buy = int(capital_required / entry_price)
    # Recalculate exact capital based on whole shares
    final_capital = shares_to_buy * entry_price
        
    return float(final_capital), shares_to_buy
