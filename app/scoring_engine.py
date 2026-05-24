def calculate_score(category, breakout_count, rsi, volume_ratio):

    score = 0

    if "High Growth" in category:
        score += 20

    if "Elite Compounder" in category:
        score += 15

    if "Mature Quality" in category:
        score += 10

    score += breakout_count * 10

    if 60 <= rsi <= 75:
        score += 15

    if volume_ratio > 2:
        score += 20

    return score
