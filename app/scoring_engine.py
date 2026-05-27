# =====================================================================================
# app/scoring_engine.py
# =====================================================================================
#
# SCORING BREAKDOWN (max 100):
#   Category   — 30 pts max  (additive for multi-category stocks)
#   Breakouts  — 30 pts max  (capped at 3 × 10, bonus for 52W)
#   RSI        — 20 pts max  (tighter sweet-spot band)
#   Volume     — 20 pts max  (higher thresholds)
#
# =====================================================================================

SCORE_CATEGORY = {
    "Elite Compounder": 30,
    "High Growth":      22,
    "Mature Quality":   14,
}

def calculate_score(category, breakout_count, rsi, volume_ratio, breakout_signals=None):

    score = 0

    # ============================================================================
    # CATEGORY — additive for multi-category stocks (e.g. Elite + High Growth = 52)
    # ============================================================================

    for label, pts in SCORE_CATEGORY.items():
        if label in category:
            score += pts

    # ============================================================================
    # BREAKOUT COUNT — 30 pts max, capped at 3
    # 52-Week breakout carries extra weight (5 bonus pts)
    # ============================================================================

    score += min(breakout_count, 3) * 10

    if breakout_signals and any("52W" in s for s in breakout_signals):
        score += 5

    # ============================================================================
    # RSI — 20 pts max
    # Sweet spot: 60-72 (momentum without being overbought)
    # ============================================================================

    if 62 <= rsi <= 72:
        score += 20
    elif 58 <= rsi < 62:
        score += 13
    elif 72 < rsi <= 78:
        score += 10
    elif 55 <= rsi < 58:
        score += 6
    elif 78 < rsi <= 82:
        score += 3

    # ============================================================================
    # VOLUME — 20 pts max, tighter thresholds
    # ============================================================================

    if volume_ratio >= 3.5:
        score += 20
    elif volume_ratio >= 2.5:
        score += 16
    elif volume_ratio >= 2.0:
        score += 12
    elif volume_ratio >= 1.5:
        score += 6
    elif volume_ratio >= 1.2:
        score += 2

    # ============================================================================
    # HARD CAP
    # ============================================================================

    return min(score, 100)
