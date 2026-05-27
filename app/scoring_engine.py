# =====================================================================================
# app/scoring_engine.py
# =====================================================================================
#
# SCORING BREAKDOWN (max 100):
#   Category   — 25 pts max  (single best match, not additive)
#   Breakouts  — 30 pts max  (capped at 3 × 10)
#   RSI        — 20 pts max  (graduated bands)
#   Volume     — 25 pts max  (graduated bands)
#
# =====================================================================================

def calculate_score(category, breakout_count, rsi, volume_ratio):

    score = 0

    # ============================================================================
    # CATEGORY — 25 pts max, best match only
    # ============================================================================

    if "Elite Compounder" in category:
        score += 25
    elif "High Growth" in category:
        score += 20
    elif "Mature Quality" in category:
        score += 10

    # ============================================================================
    # BREAKOUT COUNT — 30 pts max, capped at 3
    # ============================================================================

    score += min(breakout_count, 3) * 10

    # ============================================================================
    # RSI — 20 pts max, graduated
    # ============================================================================

    if 60 <= rsi <= 75:
        score += 20
    elif 55 <= rsi < 60:
        score += 12
    elif 75 < rsi <= 80:
        score += 10
    elif 80 < rsi <= 85:
        score += 5

    # ============================================================================
    # VOLUME — 25 pts max, graduated
    # ============================================================================

    if volume_ratio >= 3.0:
        score += 25
    elif volume_ratio >= 2.0:
        score += 20
    elif volume_ratio >= 1.5:
        score += 12
    elif volume_ratio >= 1.2:
        score += 6

    # ============================================================================
    # HARD CAP
    # ============================================================================

    return min(score, 100)
