# =====================================================================================
# app/risk_manager.py
# INTERACTIVE RISK MANAGEMENT BOT
# =====================================================================================

import logging

logger = logging.getLogger(__name__)

def calculate_position(price: float, stop_loss: float, risk_amount: float) -> dict:
    """
    Calculates exact share count based on risk tolerance.
    """
    risk_per_share = abs(price - stop_loss)
    if risk_per_share <= 0:
        return {"error": "Invalid Stop Loss distance."}
    
    shares = int(risk_amount / risk_per_share)
    capital_required = shares * price
    
    return {
        "shares": shares,
        "risk_per_share": round(risk_per_share, 2),
        "capital_required": round(capital_required, 2),
        "total_risk": round(shares * risk_per_share, 2)
    }

def handle_risk_command(symbol: str, stop_loss: float, risk_amount: float, price: float):
    """
    Formats the response for the Telegram bot.
    """
    calc = calculate_position(price, stop_loss, risk_amount)
    if "error" in calc:
        return f"❌ Error: {calc['error']}"
        
    return (
        f"🎯 <b>Risk Analysis for {symbol}</b>\n\n"
        f"Risk Amount: ₹{risk_amount}\n"
        f"Risk/Share:  ₹{calc['risk_per_share']}\n"
        f"<b>Buy Quantity: {calc['shares']} Shares</b>\n\n"
        f"Total Capital: ₹{calc['capital_required']}\n"
        f"Actual Risk:   ₹{calc['total_risk']}"
    )
