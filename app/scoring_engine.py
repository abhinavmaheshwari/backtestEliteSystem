# =====================================================================================
# app/scoring_engine.py
# =====================================================================================
#
# SCORING BREAKDOWN (max 100):
#   Category Score   — 25 pts max  (single best match, not additive)
#   Breakout Score   — 30 pts max  (capped at 3 breakouts × 10)
#   RSI Score        — 20 pts max  (graduated bands)
#   Volume Score     — 25 pts max  (graduated bands)
#
# =====================================================================================

def calculate_score(category, breakout_count, rsi, volume_ratio):

    score = 0

    # ============================================================================
    # CATEGORY — 25 pts max
    # Best single match only — not additive to prevent stacking
    # ============================================================================

    if "Elite Compounder" in category:
        score += 25
    elif "High Growth" in category:
        score += 20
    elif "Mature Quality" in category:
        score += 10

    # ============================================================================
    # BREAKOUT COUNT — 30 pts max
    # Capped at 3 breakouts to prevent runaway scores
    # ============================================================================

    score += min(breakout_count, 3) * 10

    # ============================================================================
    # RSI — 20 pts max
    # Graduated bands — sweet spot is 60–75
    # ============================================================================

    if 60 <= rsi <= 75:
        score += 20       # ideal momentum zone
    elif 55 <= rsi < 60:
        score += 12       # momentum building
    elif 75 < rsi <= 80:
        score += 10       # strong but watch for extension
    elif 80 < rsi <= 85:
        score += 5        # getting stretched

    # ============================================================================
    # VOLUME RATIO — 25 pts max
    # Graduated — rewards sustained expansion not just >2x spike
    # ============================================================================

    if volume_ratio >= 3.0:
        score += 25       # exceptional expansion
    elif volume_ratio >= 2.0:
        score += 20       # strong expansion
    elif volume_ratio >= 1.5:
        score += 12       # solid expansion
    elif volume_ratio >= 1.2:
        score += 6        # mild expansion

    # ============================================================================
    # HARD CAP — score never exceeds 100
    # ============================================================================

    return min(score, 100)
