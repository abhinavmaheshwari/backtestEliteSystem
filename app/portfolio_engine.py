import logging
from typing import Tuple
from math import floor
from app.database import get_connection

logger = logging.getLogger(__name__)

# Base capital and per-trade risk fraction.
# Default RISK_PERCENT is conservative (1% of equity at risk per trade).
BASE_CAPITAL = 500000.0
RISK_PERCENT = 0.01  # 1% of total equity risked per trade
MAX_POSITION_PCT = 0.15  # hard cap on capital allocated to a single trade (15% of equity)

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
    Risk-based sizing (institutional style):

    - We risk a fixed fraction of total equity per trade (RISK_PERCENT).
    - Per-share risk = abs(entry_price - stop_loss).
    - shares = floor(per_trade_risk / per_share_risk)
    - capital_allocated = shares * entry_price (bounded by available cash)

    Score can be used to modestly increase the risk budget for very high
    conviction trades (e.g. double the base risk for score >= 90).

    Returns (capital_allocated, shares_bought)
    """
    try:
        entry_price = float(entry_price)
        stop_loss = float(stop_loss)
    except Exception:
        return 0.0, 0

    if entry_price <= 0 or stop_loss <= 0:
        return 0.0, 0

    state = get_portfolio_state()
    total_equity = state["total_equity"]
    available_margin = state["available_margin"]

    # Base per-trade risk in dollars
    base_risk_percent = RISK_PERCENT
    if score >= 90:
        # modestly raise risk budget for extremely high-conviction ideas
        risk_percent = min(0.05, base_risk_percent * 2)
    else:
        risk_percent = base_risk_percent

    per_trade_risk = total_equity * risk_percent

    # Per-share risk (long trades assumed). If stop_loss is above entry (invalid), return 0
    per_share_risk = abs(entry_price - stop_loss)
    if per_share_risk <= 0:
        return 0.0, 0

    shares_by_risk = floor(per_trade_risk / per_share_risk)
    if shares_by_risk <= 0:
        return 0.0, 0

    capital_required = shares_by_risk * entry_price

    # Hard cap: never allocate more than MAX_POSITION_PCT of total equity to a single trade
    max_allocation = total_equity * MAX_POSITION_PCT
    if capital_required > max_allocation:
        shares_by_risk = floor(max_allocation / entry_price)
        capital_required = shares_by_risk * entry_price

    # Cap to available cash
    if capital_required > available_margin:
        shares_by_cash = floor(available_margin / entry_price)
        shares_to_buy = max(0, min(shares_by_risk, shares_by_cash))
    else:
        shares_to_buy = int(shares_by_risk)

    final_capital = float(shares_to_buy * entry_price)
    return final_capital, shares_to_buy
