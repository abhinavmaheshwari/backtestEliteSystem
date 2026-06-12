# =====================================================================================
# app/risk_manager.py  (UPGRADED v2)
#
# CHANGES FROM v1:
#   1. Accepts full result dict from compute_sl_and_target() — no manual SL passing
#   2. Shows all three targets and their individual R:R
#   3. Displays SL method and target method for transparency
#   4. Optional: portfolio % risk shown
#
# API CHANGE (update your callers):
#   v1: handle_risk_command(symbol, stop_loss, risk_amount, price)
#   v2: handle_risk_command(symbol, price, risk_amount, sl_result, portfolio=None)
# =====================================================================================

from __future__ import annotations
from typing import Optional


def calculate_position(
        price:       float,
        stop_loss:   float,
        risk_amount: float,
) -> dict:
    """
    Position sizing: given a max risk amount (INR), compute shares to buy.
    """
    risk_per_share = abs(price - stop_loss)
    if risk_per_share <= 0:
        return {"error": "Invalid Stop Loss — SL must be strictly below entry price."}

    shares           = int(risk_amount / risk_per_share)
    capital_required = round(shares * price, 2)

    return {
        "shares":           shares,
        "risk_per_share":   round(risk_per_share, 2),
        "capital_required": capital_required,
        "total_risk":       round(shares * risk_per_share, 2),
    }


def handle_risk_command(
        symbol:      str,
        price:       float,
        risk_amount: float,
        sl_result:   dict,                       # dict from compute_sl_and_target()
        portfolio:   Optional[float] = None,     # total portfolio value for % calc
) -> str:
    """
    Returns a formatted trade plan string.

    Parameters
    ----------
    symbol      : stock ticker e.g. "RELIANCE"
    price       : entry price (candle close at signal bar)
    risk_amount : max INR to risk on this trade
    sl_result   : dict returned by compute_sl_and_target()
    portfolio   : optional total portfolio INR → shows % risk
    """
    if "error" in sl_result:
        return f"Error: {sl_result['error']}"

    stop_loss = sl_result["stop_loss"]
    calc      = calculate_position(price, stop_loss, risk_amount)

    if "error" in calc:
        return f"Error: {calc['error']}"

    t1   = sl_result.get("target_1")
    t2   = sl_result.get("target_2")
    t3   = sl_result.get("target_3")
    rr   = sl_result.get("rr_ratio", 0)
    risk = sl_result.get("risk", abs(price - stop_loss))

    def rr_label(target):
        if target and risk > 0:
            return f"  ({round((target - price) / risk, 1)}:1 RR)"
        return ""

    lines = [
        f"Trade Plan — {symbol}",
        f"{'─' * 42}",
        f"Entry Price      : INR {price}",
        f"Stop Loss        : INR {stop_loss}  (INR {risk}/share)",
        f"  Method         : {sl_result.get('sl_method', 'ATR-based')}",
        f"",
        f"Target 1         : INR {t1}{rr_label(t1)}  ← primary exit",
    ]

    if t2:
        lines.append(f"Target 2         : INR {t2}{rr_label(t2)}")
    if t3:
        lines.append(f"Target 3         : INR {t3}{rr_label(t3)}  ← momentum target")

    lines += [
        f"  Method         : {sl_result.get('t_method', 'RR-based')}",
        f"",
        f"RSI Zone         : {sl_result.get('rsi_zone', 'N/A').upper()}",
        f"Overall R:R (T1) : {rr}:1",
        f"{'─' * 42}",
        f"Risk Amount      : INR {risk_amount:,.0f}",
        f"Risk / Share     : INR {calc['risk_per_share']}",
        f"Buy Quantity     : {calc['shares']} shares",
        f"Capital Required : INR {calc['capital_required']:,.2f}",
        f"Actual Risk      : INR {calc['total_risk']:,.2f}",
    ]

    if portfolio and portfolio > 0:
        pct = round(calc["total_risk"] / portfolio * 100, 2)
        lines.append(f"Portfolio Risk   : {pct}% of INR {portfolio:,.0f}")

    return "\n".join(lines)
