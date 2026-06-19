#!/bin/bash

echo "🚀 Starting Elite Breakout System Supervisor..."

while true; do
    echo "▶️ Launching app/main.py..."
    python3 app/main.py
    EXIT_CODE=$?
    
    echo "⚠️ main.py crashed or exited with code $EXIT_CODE."
    
    if [ $EXIT_CODE -eq 0 ]; then
        echo "🛑 Clean exit detected. Stopping supervisor loop."
        break
    fi
    
    echo "⏳ Respawning in 5 seconds to allow Railway health checks and port releases..."
    sleep 5
done
