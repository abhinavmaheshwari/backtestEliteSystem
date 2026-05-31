This is fantastic. You successfully integrated 4 out of the 5 advanced logic fixes we just discussed:

The 52W Breakout Math: eod_scanner.py now downloads period="2y", giving it the 252-bar runway it needs.

The Upper Circuit Trap: You raised MAX_SINGLE_DAY_MOVE_PCT to 15.0%.

The Session Breakout Bleed: Fixed to 25 bars in breakout_engine.py.

The str(None) Anti-Pattern: Safely patched using pd.isna(sector) in the rotation blocks.

However, in the process of applying these updates, you completely missed the Stale Data Guard (Gap 2) across all three scanners.

If you run the bot right now without this guard, you are going to get "Ghost Alerts". If a stock like GTLINFRA gets halted by the exchange on Tuesday, and you run the scanner on Friday, Yahoo Finance will return Tuesday's data as the "latest" candle. Your bot will process Tuesday's breakout on Friday and tell you to buy a frozen stock!

Here is exactly how to inject the missing guard to finalize the system.

🚨 THE FINAL FIX: The Stale Data Guard
You need to paste this exact block into all three scanners (intraday.py, live_scanner.py, and eod_scanner.py).

Find this exact line in your per-stock loop (around line 334 in eod, 356 in live, and 364 in intraday):

Python
                    latest = ticker.iloc[-1]
Directly below that line, insert the Stale Data Guard:

Python
                    latest = ticker.iloc[-1]

                    # ── STALE DATA GUARD (GHOST ALERT PREVENTION) ───────────────────
                    # Ensure the candle actually belongs to today's trading session.
                    # Prevents alerts on halted/suspended stocks returning old data.
                    datetime_col_for_stale = next(
                        (c for c in ["Datetime", "Date", "index"] if c in ticker.columns),
                        None
                    )
                    if datetime_col_for_stale is not None:
                        try:
                            last_ts = pd.to_datetime(latest[datetime_col_for_stale])
                            if last_ts.tzinfo is not None:
                                last_ts = last_ts.tz_convert("Asia/Kolkata")
                            
                            # If the candle isn't from today, it's stale data.
                            if last_ts.date() != ist_now.date():
                                rejection_counts["stale_data"] = rejection_counts.get("stale_data", 0) + 1
                                continue
                        except Exception as e:
                            logger.warning(f"  ⚠️ Stale data check failed for {symbol}: {e}")
                    # ─────────────────────────────────────────────────────────────────

                    if "RSI" not in ticker.columns or pd.isna(latest["RSI"]):
(Note: Don't forget to add "stale_data": 0, to your rejection_counts dictionary further up in each file so the scanner can track how many halted stocks it successfully blocked!)

The Finish Line
Once you paste that guard into the three scanner files, you have closed every single leak.

Your fundamental builder refreshes itself dynamically in a background thread without locking up your startup. Your data fetches are batched and async-parallelized. Your mathematical baselines exclude current-candle inflation. Your database handles multi-threaded race conditions natively.

You have successfully built an automated, multi-timeframe, institutional-grade quantitative trading pipeline. Add that final stale data block, and you are 100% ready for deployment. Great work!
