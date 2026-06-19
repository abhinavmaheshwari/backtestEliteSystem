#!/bin/bash
set -e

# If the user doesn't provide a DB, use a local backtest SQLite equivalent or a local Postgres.
# Since the app requires Postgres, we will fallback to a local pg connection if BACKTEST_DATABASE_URL is not set.
# You can run `docker run --name backtest-db -e POSTGRES_PASSWORD=postgres -p 5432:5432 -d postgres` to start a local DB.

if [ -z "$BACKTEST_DATABASE_URL" ]; then
    echo "⚠️ BACKTEST_DATABASE_URL not set."
    echo "Make sure you have a local Postgres instance running. Using default local URL."
    export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/postgres"
else
    export DATABASE_URL="$BACKTEST_DATABASE_URL"
fi

export BACKTEST_MODE=true
export FLASK_ENV=development

echo "=========================================================================="
echo "🚀 Launching Elite System Dashboard against BACKTEST DB"
echo "🌐 DATABASE_URL: $DATABASE_URL"
echo "=========================================================================="

python -m flask --app app.dashboard_server run --port 5001

