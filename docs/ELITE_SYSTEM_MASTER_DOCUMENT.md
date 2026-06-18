# ELITE BREAKOUT SYSTEM - MASTER SYSTEM ARCHITECTURE & QUANTITATIVE WHITEPAPER

**Target Audience:** Techno-Fundamental Analysts, Quantitative Researchers, and Portfolio Managers.
**Purpose:** This document is the ultimate, exhaustive reference for the Elite Breakout System. It merges the **exact technical architecture** (database schemas, execution threads, config parameters, python conditions) with the **quantitative financial rationale** behind every parameter. It is designed to pass rigorous due diligence from both a software engineering and a hedge fund auditing perspective.

---

## 1. System Architecture & Execution Flow

### 1.1 Threading Model (`main.py`)
The system is designed with a multi-threaded architecture running continuously:
- **Main Thread**: Runs a Flask Web Dashboard on port `8080`.
- **Scheduler Thread**: Custom `run_system_scheduler()` running an infinite loop every 30 seconds to trigger time-based events.
- **Daemon Threads**:
  - `EOD Scanner`: `6:30 PM - Midnight` (Retries every 60s on failure, force stops at midnight).
  - `Reversal Scanner`: `6:30 PM - Midnight`.
  - `Intraday Scanner`: `9:32 AM - 3:30 PM` (15m candles, continuous loop with 5 min sleep).
  - `Live Scanner`: `10:17 AM - 3:30 PM` (1h candles).
  - `Wealth Engine`: Initial run at `1:05 AM`, then every 30 mins between `10:00 AM - 3:30 PM`.
- **Watchdog Thread**: Runs every 15 minutes to restart crashed scanners (unless they are ONE_SHOT like EOD/Reversal).

### 1.2 Data Flow & External APIs
1. **TradingView Query API**: Used at `1:00 AM` to fetch fundamentals for ~5,000 NSE stocks.
2. **NSE APIs (Surveillance)**: `https://www.nseindia.com/api/reportASM` and `reportGSM` to blacklist toxic stocks.
3. **Yahoo Finance (`yfinance`)**: Primary price fetcher with 60-second TTL cache for intraday, 1-day for EOD.
4. **NSE Delivery API**: Fetches institutional delivery percentage from bhavcopy.
5. **AI Concall API**: Cached AI analysis for management guidance.

---

## 2. Global Configuration Constants (`config.py`)

### 2.1 Scanner Breakout Thresholds
- **15m Scanner**: Target Score `> 78` | Max ATR Target: `5.0x` | Min Vol: `150,000` | Breakout Margin: `0.3%`
- **1h Scanner**: Target Score `> 80` | Max ATR Target: `8.0x` | Min Vol: `100,000` | Breakout Margin: `0.5%`
- **1d Scanner (EOD)**: Target Score `> 82` | Max ATR Target: `12.0x` | Min Vol: `50,000` | Breakout Margin: `0.7%`

### 2.2 Global Limits & Filters
- `MIN_STOCK_PRICE`: ₹100.0 (No penny stocks)
- `ADX_MIN_THRESHOLD`: 25 (Minimum ADX for Daily timeframe)
- `MIN_BREAKOUT_VOLUME_RATIO`: 1.5x (Volume confirmation threshold)
- `MAX_PROMOTER_PLEDGE`: 20% (>20% triggers automatic disqualification)
- `MIN_DAILY_LIQUIDITY_RUPEES_WATCHLIST`: ₹150,000,000 (₹15 Cr/day)
- `MIN_DAILY_LIQUIDITY_RUPEES_WEALTH`: ₹10,000,000 (₹1 Cr/day)
- `BASE_TIGHTNESS_THRESHOLD`: 1.5 (For pre-breakout consolidation bonus)
- `BASE_VOLATILITY_THRESHOLD`: 3.0 (For choppy base penalty)

---

## 3. Database Schema Details (`database.py`)

The backend is built on PostgreSQL, ensuring ACID compliance for trade logging and system state.

### 3.1 `alerts` Table (Technical Breakouts)
- `id` (SERIAL PRIMARY KEY)
- `symbol` (TEXT, NOT NULL)
- `breakout_type` (TEXT, NOT NULL)
- `alert_time` (TEXT, NOT NULL) - ISO format
- `alert_date` (TEXT, DEFAULT CURRENT_DATE)
- `scanner` (TEXT) - 'INTRADAY', 'EOD', 'REVERSAL', '1H', 'LIVE'
- `category` (TEXT) - Extracted fundamental category
- `entry_price` (REAL)
- `stop_loss` (REAL)
- `target_price` (REAL)
- `signals` (TEXT)
- `score` (INTEGER)
- `rsi` (REAL)
- `volume_ratio` (REAL)
- `status` (TEXT) - 'OPEN', 'CLOSED', 'EXITED'
- `exit_price` (REAL)
- `pnl_pct` (REAL)
- `capital_allocated` (REAL)
- `shares_bought` (INTEGER)

### 3.2 `wealth_buy_alert` Table (Positional Signals)
- `id` (SERIAL PRIMARY KEY)
- `symbol` (TEXT)
- `alert_price` (REAL)
- `alert_date` (DATE)
- `breakout_type` (TEXT)
- `fm_score` (INTEGER)
- `position_pct` (REAL)
- `position_amount` (REAL)
- `portfolio_bucket` (TEXT)
- `status` (TEXT) - 'ACTIVE', 'BUY', 'SELL', 'HOLD'
- `current_price` (REAL)
- `exit_signal` (TEXT)
- `exit_price` (REAL)
- `is_closed` (BOOLEAN, DEFAULT FALSE)
- `valuation_score` (INTEGER)
**Unique Constraint**: `(symbol, alert_date)`

### 3.3 Operational Tables
- **`scanner_health`**: Tracks `scanner_name`, `status` (OK/DOWN/IDLE), `last_success`, `today_alerts`, `error_msg`, `retry_count`.
- **`fetch_errors`**: Tracks API failures. `source_name`, `scanner_name`, `symbol`, `interval`, `category`, `occurrences` (upsert increment).
- **`score_weight_log`**: Tracks Bayesian Model weights over time (`model_version`, `regime`, `weights` JSONB).

---

## 4. Daily Watchlist Builder (`daily_builder.py`)

Executes daily at 1:00 AM to filter ~5,000 stocks down to ~341 elite stocks.

### 4.1 Initial API Filter & Liquidity
- Exchange == "NSE"
- Close >= `100`
- Market Cap >= `10,000,000,000` (₹1,000 Cr)
- Basic EPS TTM > `0`
- ROE >= `8%`

**Financial Rationale:** We enforce a strict floor on liquidity (₹15 Cr/day) and Market Cap (₹1,000 Cr). Institutional capital cannot enter or exit smaller stocks without suffering severe slippage/impact costs.

### 4.2 "Junk-Kill" Gates (Hard Exclusions)
If a stock violates any of these, the system's logic instantly halts evaluation (`return skip(...)`).
1. **Leverage Gate**: `Debt_to_Equity > 1.0` (Allowed up to `2.5` for Utilities).
   - *Rationale*: High leverage destroys equity value during interest rate tightening cycles. We minimize insolvency risk.
2. **Profitability Gate**: `Operating_Margin (OPM) < 0%`.
   - *Rationale*: Negative OPM indicates the core business model is burning cash before accounting for debt service or taxes.
3. **Structural Collapse Gate**: `YoY_Revenue < -20% AND YoY_Profit < -20%`.
   - *Rationale*: A simultaneous collapse in both top and bottom lines indicates a structural industry shift or severe loss of market share.
4. **Toxic Lending Gate (Financials Only)**: `ROA < 0.3%`.
   - *Rationale*: For banks/NBFCs, an ROA below 0.3% almost guarantees heavily under-provisioned Non-Performing Assets (NPAs).
5. **Surveillance Blacklist**: Symbol exists in NSE ASM/GSM lists.

### 4.3 Fundamental Classification Categories
Stocks are split into **Path A** (Non-Financials) and **Path B** (Financials).

**Path A (Non-Financials)**:
- **High Momentum**: YoY Sales > `15%`, YoY Profit > `15%`, Margin Expansion.
- **Wealth Compounder**: YoY Sales > `3%`, YoY Profit > `3%`, ROE >= `15%`, OPM >= `12%`, D/E <= `1.0`.
- **Debt-Free Cash Generator**: D/E <= `0.1`, ROE >= `20%`, Market Cap >= ₹10,000 Cr, FCF Margin > `0`. *Rationale*: Zero bankruptcy risk, accounting profits backed by cash.
- **Undervalued Growth**: YoY Sales > `15%`, YoY Profit > `15%`, PEG between `0.0` and `1.0`. *Rationale*: GARP (Growth at a Reasonable Price).
- **Capital Efficient**: ROE >= `25%`, OPM >= `15%`, D/E <= `1.0`. *Rationale*: Asset-light compounders that don't require debt financing.
- **Recovery Play**: YoY Profit >= `30%`, YoY Sales >= `-10%`, Margin Expansion. *Rationale*: Operating leverage kicking in post-bottom.
- **Dividend Aristocrat**: Dividend Yield >= `3.0%`, ROE >= `15%`, D/E <= `1.0`, Market Cap >= ₹5,000 Cr.

**Path B (Financials - Banks & NBFCs)**:
- **Top Bank/NBFC**: YoY Rev > `5%`, YoY Profit > `5%`, ROE >= `15%`, ROA >= `1.0%`.
- **Efficient Lender**: ROA >= `2.0%`, ROE >= `15%`, Market Cap >= ₹5,000 Cr. *Rationale*: An ROA > 2% in Indian banking signifies exceptional Net Interest Margins (NIM) and pristine asset quality.

---

## 5. Wealth Engine & Capital Allocation (`wealth_engine.py`)

Runs every 30 minutes. Generates `FM_Score` (Fund Manager Score) and assigns buckets.

### 5.1 The 100-Point FM_Score Rubric
- **Quality (25 pts)**: ROE >= `15%` (+8), ROCE >= `20%` (+9), D/E <= `0.1` (+8).
- **Growth (25 pts)**: YoY Sales >= `20%` (+13), YoY Profit >= `20%` (+12).
- **Valuation (10 pts)**: PEG Ratio < `1.0` (+6), P/E discount to Sector Median > `15%` (+4). *Rationale*: The Anti-Bubble Mechanism prevents buying "Quality at Any Price".
- **Momentum (20 pts)**: RS_Rating > `90` (+8), Dist to 52W High <= `5%` (+5), Price > SMA_200 (+5). *Rationale*: Fundamentals tell us *what* to buy; Momentum tells us *when* to buy.
- **Ownership (10 pts)**: "Inst Accumulation" flag present (+10).
- **Cash Flow Quality (10 pts)**: FCF Margin >= (OPM * 0.5) (+10). *Rationale*: Validates that Operating Profits are backed by actual cash, catching Satyam/DHFL-type aggressive revenue recognition.
- **AI Sentiment (±5 pts)**: AI Confidence >= `8` (+5), AI Confidence <= `4` (-5).

### 5.2 Dynamic Position Sizing (Conviction-Proportional Sizing)
The system calculates the exact ₹ capital to allocate based on conviction.
- **Logic**: `Conviction Multiplier = MAX(0.5, (FM_Score - 60) / 40)`
- **Base Allocations**: Core Bucket = 3%, Growth = 4%, Opportunistic = 1.5%.
- **Final Allocation**: `Position_PCT = MIN(Base * Conviction_Multiplier, 0.05)`
- *Rationale*: Maximum capital (up to 5% cap) goes to 90-100 score stocks, while aggressively tapering capital for borderline setups (scores 60-70).

### 5.3 Hold Score & Tax-Loss Harvesting (Exits)
Existing positions are evaluated using a 100-point `Hold_Score`.
- **Circuit Breaker**: If Unrealized Drawdown > `30%` -> Instant `0` score (SELL). If > `20%` -> `-25` pts.
- **Sell Trigger**: If `Hold_Score` < `45` -> "SELL REVIEW". Catastrophic RS < `-40` triggers instant "SELL".
- **Tax-Loss Harvest**: `if Unrealized_PnL < -10% AND Days_Held < 365: return "HOLD (Tax-Loss Harvest)"`.
- **LTCG Bonus**: `if Days_To_LTCG <= 30: Hold_Score += (10 * (Days_To_LTCG / 30))`. *Rationale*: If a stock is technically weakening but 30 days away from Long-Term Capital Gains (12.5% vs 20% tax), the system mathematically boosts the hold score because the tax benefit outweighs the minor technical breakdown.

---

## 6. Breakout Detection Rules (`breakout_engine.py`)

To fire a raw technical signal, the candle must pass specific anti-fake-breakout gates:
1. **Statistical Volume Anomalies**: `Z-Score = (Current_Volume - μ_20) / σ_20`. Must exceed `2.5` (15m), `2.0` (1h), `1.8` (1d). *Rationale*: Only institutional volume creates a 2+ standard deviation anomaly.
2. **Closing Confirmation**: 
   - `Close > Prior_High * (1 + Breakout_Margin)`
   - `Low > Prior_High * 0.997`
   - *Rationale*: Ignores wicks. The candle *body* must establish itself cleanly above resistance to prevent "fake-outs" and stop-hunts.
3. **Quality Multipliers**: Tight Base (Base Width < 1.5) multiplies score by 1.5x. OBV Divergence (Bearish OBV) multiplies by 0.5x.

---

## 7. Scoring Engine & Hard Disqualifiers (`scoring_engine.py`)

A breakout signal is piped here. **If a stock hits any of the 11 Hard Disqualifiers, the score is instantly set to 0 and the trade is vetoed.**

### 7.1 The 11 Hard Disqualifiers (Instant 0) & Market Mechanics

1. **Distribution Candle (Operator Trap)**
   - *Logic*: `if Volume_Ratio >= 2.0 AND Close < Midpoint_Of_Candle: return 0`
   - *Rationale*: Massive volume closing near the lows implies institutional sellers are dumping inventory into retail demand.
2. **Rejection Wick**
   - *Logic*: `if (High - Close) / (High - Low) > 0.40: return 0`
   - *Rationale*: Buyers pushed the price up, but aggressive institutional selling absorbed the demand and forced it back down.
3. **Weak Directional Index (ADX)**
   - *Logic*: `if ADX < 25 (Daily timeframe): return 0`
   - *Rationale*: ADX < 25 indicates a choppy, mean-reverting environment where breakouts have an 80% failure rate.
4. **Bearish RSI Divergence**
   - *Logic*: `if Price_Now > Price(6 bars ago) AND RSI_Now < RSI(6 bars ago) - 3: return 0`
   - *Rationale*: Price is making a higher high, but momentum is making a lower high. Reversal is imminent.
5. **Baseless Overextension (Rubber Band Effect)**
   - *Logic*: `if Price > Upper_Bollinger_Band AND Volume_Ratio < 2.0 (Daily): return 0`
   - *Rationale*: Price stretched 2 standard deviations away from the mean without massive institutional volume to sustain it will snap back.
6. **Pre-Breakout Exhaustion**
   - *Logic*: `if 3 of last 4 candles have (Body / Total_Range) < 0.25: return 0`
   - *Rationale*: Doji candles represent indecision. The stock has exhausted its momentum battling resistance.
7. **The Climax Top (Smart Money Exit)**
   - *Logic*: `if Volume == Max(last 20 bars) AND Upper_Wick > 25% AND Close in bottom 40%: return 0`
   - *Rationale*: The signature blow-off top. Retail FOMO drives max volume, institutions unload, causing a weak close.
8. **Lower-High Pattern (Failed Retest)**
   - *Logic*: `if High_Now < High(3 bars ago) < High(6 bars ago): return 0`
   - *Rationale*: Descending peaks mean supply is overwhelming demand. The trend has structurally reversed.
9. **Thin Spread Trap**
   - *Logic*: `if Candle_Range / Close_Price < 0.003: return 0`
   - *Rationale*: Moving less than 0.3% is statistical noise and indicates low liquidity manipulation.
10. **Piotroski F-Score Failure (EOD Only)**
    - *Logic*: `if Piotroski_F_Score <= 3: return 0`
    - *Rationale*: A score <= 3 indicates severe financial distress. We do not buy positional breakouts in distressed companies.
11. **Illiquidity**
    - *Logic*: 20-bar avg volume < `50,000` (EOD), `100,000` (1h), `150,000` (15m).

### 7.2 Bonus & Penalty Modifiers (Additive Points)
- **FII Block Deal Footprint (+8 pts)**: System detected an actual Foreign Institutional Investor bulk deal via NSE feeds.
- **Tight Base Consolidation (+4 pts)**: Base Width < 1.5%. Volatility contraction signals supply absorption.
- **ATR Quality Move (+3 pts)**: (EOD) Candle range between 1.0x - 2.0x ATR.
- **Stage 4 / Late Stage Base Penalties (-10 to -15 pts)**: Price > 150% above 52W Low, or 200 SMA declining YoY. *Rationale*: Protects against late-stage distribution (Stage 3/4) where risk is heavily skewed to the downside.
- **Extended from SMA50 Penalty (-6 pts)**: > `5%` above SMA50. Prevents chasing extended stocks far from trend support.
- **Unsustained Volume Penalty (-8 pts)**: < 2 of last 3 bars have 80% avg vol. Breakout lacks follow-through conviction.

---
*Generated by Antigravity*
