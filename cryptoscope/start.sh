#!/bin/bash
# MEANX startup script
# 1. Rebuild DB if needed
# 2. Auto-compute pairs if empty
# 3. Load hourly candles
# 4. Ensure favorites storage
# 5. Load and refresh RU market
# 6. Load Brazil market
# 7. Load Indonesia market
# 8. Start background updater loop
# 9. Launch app

set -e

export DB_PATH="${DB_PATH:-/data/market.db}"
export PORT="${PORT:-3000}"
export PYTHONPATH="/app:${PYTHONPATH:-}"
export CSV_PATH="${CSV_PATH:-/opt/seed/all_markets_3yr.csv}"
export RU_CSV_PATH="${RU_CSV_PATH:-/opt/seed/tinkoff_ru_2yr.csv}"
export HOURLY_PATH="${HOURLY_PATH:-/opt/seed/hourly_6coins_2yr.csv}"
if [ -z "${ENABLED_MARKETS:-}" ]; then
    case "${APP_VARIANT:-global}" in
        br) export ENABLED_MARKETS="crypto,stocks,br" ;;
        id) export ENABLED_MARKETS="crypto,stocks,id" ;;
        *) export ENABLED_MARKETS="crypto,stocks,ru,br,id" ;;
    esac
fi

market_enabled() {
    case ",$ENABLED_MARKETS," in
        *",$1,"*) return 0 ;;
        *) return 1 ;;
    esac
}

echo "=== MEANX Starting ==="
echo "DB_PATH=$DB_PATH"
echo "PORT=$PORT"
echo "APP_VARIANT=${APP_VARIANT:-global}"
echo "ENABLED_MARKETS=$ENABLED_MARKETS"

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

# 4. Ensure favorites table and migrations
python /scripts/load_favorites.py

# 5. Load and refresh Russian stocks
if market_enabled "ru"; then
    python /scripts/load_ru.py
    python /scripts/update_ru.py
    # Recompute if RU data exists but RU pairs are missing.
    RU_COUNT=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM prices WHERE market='ru';" 2>/dev/null || echo "0")
    RU_PAIR_COUNT=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM pairs WHERE market='ru';" 2>/dev/null || echo "0")
    if [ "$RU_COUNT" -gt 0 ] && [ "$RU_PAIR_COUNT" -lt 1 ]; then
        python /scripts/compute_analysis.py
    fi
fi

# 6. Load Brazil B3 stocks
if market_enabled "br"; then
    python /scripts/load_brazil.py
    BR_COUNT=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM prices WHERE market='br';" 2>/dev/null || echo "0")
    BR_PAIR_COUNT=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM pairs WHERE market='br';" 2>/dev/null || echo "0")
    if [ "$BR_COUNT" -gt 0 ] && [ "$BR_PAIR_COUNT" -lt 1 ]; then
        python /scripts/compute_analysis.py
    fi
fi

# 7. Load Indonesia IDX stocks
if market_enabled "id"; then
    python /scripts/load_indonesia.py
    ID_COUNT=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM prices WHERE market='id';" 2>/dev/null || echo "0")
    ID_PAIR_COUNT=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM pairs WHERE market='id';" 2>/dev/null || echo "0")
    if [ "$ID_COUNT" -gt 0 ] && [ "$ID_PAIR_COUNT" -lt 1 ]; then
        python /scripts/compute_analysis.py
    fi
fi

# 8. Start background update loop (checks every 30s, runs daily_update at 06:00 UTC)
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

# 9. Launch FastAPI app
echo "Starting MEANX on port $PORT..."
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
