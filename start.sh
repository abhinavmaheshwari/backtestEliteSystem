#!/bin/bash
# Decouple the trading watchdog from the Flask web server
# The worker runs in the background with a supervisor loop and the web server runs in the foreground.

echo "Starting ELITE BREAKOUT SYSTEM in decoupled mode..."

# Supervisor loop — restarts worker on crash
while true; do
    python app/main.py --worker &
    WORKER_PID=$!
    trap "kill $WORKER_PID 2>/dev/null; exit 0" EXIT SIGTERM SIGINT
    wait $WORKER_PID
    EXIT_CODE=$?
    echo "[start.sh] Worker exited with code $EXIT_CODE. Restarting in 10s..."
    sleep 10
done &

# Start the Flask dashboard using Gunicorn (production WSGI)
# Railway exposes the port dynamically
exec gunicorn --bind 0.0.0.0:$PORT --workers 2 --timeout 120 --preload app.dashboard_server:app
