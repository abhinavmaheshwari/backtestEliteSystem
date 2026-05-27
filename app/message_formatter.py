# =====================================================================================
# app/message_formatter.py
# TELEGRAM MESSAGE FORMATTER
# Rules: no box-drawing chars, no block chars — emoji + hyphens only
# =====================================================================================

# =====================================================================================
# SCORE COSMETICS
# =====================================================================================

def score_tier(score):
    if score >= 95: return "ELITE ★★★"
    if score >= 85: return "STRONG ★★"
    if score >= 75: return "SOLID ★"
    return             "WATCH"

def score_badge(score):
    if score >= 95: return "🏆"
    if score >= 85: return "🔥"
    if score >= 75: return "⚡"
    return             "📌"

def score_bar(score):
    # 10 green circles filled, grey unfilled — renders cleanly on all Telegram clients
    filled = round(score / 10)
    return "🟢" * filled + "⚫" * (10 - filled)

# =====================================================================================
# CATEGORY — compact
# =====================================================================================

_CAT_ICON = {
    "Elite Compounder": "💎",
    "High Growth":      "📈",
    "Mature Quality":   "🏛",
}

def category_icons(category):
    return "  ".join(
        _CAT_ICON.get(c.strip(), c.strip())
        for c in category.split("+")
    )

def category_short(category):
    parts = []
    for c in category.split("+"):
        c = c.strip()
        icon = _CAT_ICON.get(c, "")
        label = c.replace("Elite Compounder","Elite").replace("High Growth","Growth").replace("Mature Quality","Quality")
        parts.append(f"{icon} {label}")
    return "  |  ".join(parts)

# =====================================================================================
# BREAKOUT SIGNALS — one per line with emoji
# =====================================================================================

_BK_ICON = {
    "52W Breakout":     "🚀",
    "Monthly Breakout": "🌕",
    "Weekly Breakout":  "📊",
    "Daily Breakout":   "📉",
}

def breakout_lines(signals):
    return "\n".join(
        f"  {_BK_ICON.get(s, '•')} {s}"
        for s in signals
    )

# =====================================================================================
# SINGLE ALERT BLOCK
#
# Example output:
#
# - - - - - - - - - - - - - - - -
# 🏆 HINDZINC  |  100/100  ELITE ★★★
# 🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢
# ₹656.1  |  RSI 70.7  |  Vol 1.9x  |  Body 65%
# 💎 Elite  |  📈 Growth
#   🌕 Monthly Breakout
#   📊 Weekly Breakout
# =====================================================================================

_DIV = "- " * 16      # "- - - - - - - - - - - - - - - - "

def format_alert(a):
    badge = score_badge(a["score"])
    tier  = score_tier(a["score"])
    bar   = score_bar(a["score"])
    cat   = category_short(a["category"])
    bk    = breakout_lines(a["breakout_signals"])

    return "\n".join([
        _DIV,
        f"{badge} {a['symbol']}  |  {a['score']}/100  {tier}",
        bar,
        f"₹{a['price']}  |  RSI {a['rsi']}  |  Vol {a['volume_ratio']}x  |  Body {a['body_ratio']}%",
        cat,
        bk,
    ])

# =====================================================================================
# FULL MESSAGE
# =====================================================================================

_HEADER = {
    "EOD":      "📊 EOD DAILY SCAN",
    "1H":       "🚀 TREND SCAN 1H",
    "INTRADAY": "⚡ INTRADAY 15M",
}

_TOP = "= = = = = = = = = = = = = = = = ="

def build_message(scanner, cat, alerts, chunk_num, total_chunks, scan_time):
    suffix    = f"  [{chunk_num}/{total_chunks}]" if total_chunks > 1 else ""
    cat_icons = category_icons(cat)
    header    = "\n".join([
        _TOP,
        f"{_HEADER.get(scanner, scanner)}{suffix}",
        f"{cat_icons}  |  {len(alerts)} alert{'s' if len(alerts) != 1 else ''}",
        _TOP,
    ])

    body = "\n\n".join(format_alert(a) for a in alerts)

    return f"{header}\n\n{body}\n\n⏰ {scan_time}"
