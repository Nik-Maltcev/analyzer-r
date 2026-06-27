#!/bin/bash
# CryptoScope startup script
# 1. Rebuild DB if needed
# 2. Auto-compute pairs if empty
# 3. Load hourly candles
# 4. Load RU market
# 5. Load Brazil market
# 6. Ensure favorites storage
# 7. Start background updater loop
# 8. Launch app

set -e

export DB_PATH="${DB_PATH:-/data/market.db}"
export PORT="${PORT:-3000}"
export PYTHONPATH="/app:${PYTHONPATH:-}"
export CSV_PATH="${CSV_PATH:-/opt/seed/all_markets_3yr.csv}"
export RU_CSV_PATH="${RU_CSV_PATH:-/opt/seed/tinkoff_ru_2yr.csv}"
export HOURLY_PATH="${HOURLY_PATH:-/opt/seed/hourly_6coins_2yr.csv}"

echo "=== CryptoScope Starting ==="
echo "DB_PATH=$DB_PATH"
echo "PORT=$PORT"

# 1. Rebuild DB if needed
if [ ! -f "$DB_PATH" ] || [ ! -s "$DB_PATH" ]; then
    echo "DB missing or empty, rebuilding..."
    python /scripts/build_db.py
else
    # Check if prices table has data
    ROW_COUNT=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM prices;" 2>/dev/null || echo "0")
    if [ "$ROW_COUNT" -lt 1 ] && [ -f "$CSV_PATH" ]; then
        echo "DB has no price data, rebuilding..."
        python /scripts/build_db.py
    fi
fi

# 2. Auto-compute pairs if empty
PAIR_COUNT=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM pairs;" 2>/dev/null || echo "0")
if [ "$PAIR_COUNT" -lt 1 ]; then
    echo "No pair analysis, computing..."
    python /scripts/compute_analysis.py
fi

# 3. Load hourly candles
python /scripts/load_hourly.py

# 4. Load Russian stocks
python /scripts/load_ru.py
if [ $? -eq 0 ]; then
    # Recompute if RU data exists but RU pairs are missing.
    RU_COUNT=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM prices WHERE market='ru';" 2>/dev/null || echo "0")
    RU_PAIR_COUNT=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM pairs WHERE market='ru';" 2>/dev/null || echo "0")
    if [ "$RU_COUNT" -gt 0 ] && [ "$RU_PAIR_COUNT" -lt 1 ]; then
        python /scripts/compute_analysis.py
    fi
fi

# 5. Load Brazil B3 stocks
python /scripts/load_brazil.py
BR_COUNT=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM prices WHERE market='br';" 2>/dev/null || echo "0")
BR_PAIR_COUNT=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM pairs WHERE market='br';" 2>/dev/null || echo "0")
if [ "$BR_COUNT" -gt 0 ] && [ "$BR_PAIR_COUNT" -lt 1 ]; then
    python /scripts/compute_analysis.py
fi

# 6. Ensure favorites table
python /scripts/load_favorites.py

# 7. Start background update loop (checks every 30s, runs daily_update at 06:00 UTC)
(
    while true; do
        CURRENT_HOUR=$(date -u +%H)
        CURRENT_MINUTE=$(date -u +%M)
        if [ "$CURRENT_HOUR" = "06" ] && [ "$CURRENT_MINUTE" = "00" ]; then
            echo "[$(date -u)] Running daily update..."
            python /scripts/daily_update.py || echo "[$(date -u)] daily update failed"
            sleep 90
        fi
        sleep 30
    done
) &

# 8. Launch FastAPI app
echo "Starting CryptoScope on port $PORT..."
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
