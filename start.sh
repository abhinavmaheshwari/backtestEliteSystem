#!/bin/bash

# Force backtest mode globally
export BACKTEST_MODE=true
export PYTHONPATH="$(pwd)/app:$(pwd)"

echo "=========================================================================="
echo "🚀 Starting Elite Backtest Engine on Railway"
echo "=========================================================================="

# 1. Start the Flask Dashboard in the background
# Railway requires a service to bind to the $PORT within 60s, or it marks the deploy as failed.
# Starting the dashboard first guarantees the deploy succeeds.
echo "🌐 Starting Dashboard Server..."
PYTHONPATH="$(pwd)/app" python3 -m flask --app app.dashboard_server run --host 0.0.0.0 --port "${PORT:-8080}" &
DASHBOARD_PID=$!

# 2. Run the heavy backtest simulation sequentially in the background
# This will log to the Railway console while the dashboard remains accessible.
echo "⏳ Launching Backtest Simulation pipeline..."
(
    echo "[1/2] Pre-fetching historical cache (this prevents yfinance IP bans)..."
    python3 app/prefetch_cache.py
    
    echo "[2/2] Running Backtest Time-Machine..."
    python3 backtest_engine.py
    
    echo "✅ Backtest Simulation Pipeline Complete!"
) &

# Wait for the web server to keep the container alive
wait $DASHBOARD_PID

