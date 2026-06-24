#!/bin/bash

DB_PATH="${DB_PATH:-/data/market.db}"
SEED_CSV="${CSV_PATH:-/opt/seed/all_markets_3yr.csv}"

echo "[start.sh] DB_PATH=${DB_PATH}"
echo "[start.sh] SEED_CSV=${SEED_CSV}"

# Save env vars for cron
env | grep -E '^[A-Za-z_][A-Za-z_0-9]*=' > /etc/environment || true

# Railway mounts a fresh volume over /data, hiding the build-time DB.
# Also rebuild if the DB file is missing, empty, or has no prices table.
needs_build=0
if [ ! -f "$DB_PATH" ]; then
  echo "[start.sh] DB not found -> rebuild"
  needs_build=1
elif [ ! -s "$DB_PATH" ]; then
  echo "[start.sh] DB is empty (0 bytes) -> rebuild"
  needs_build=1
else
  # Check prices table has rows (SQLite CLI may be absent; use R one-liner)
  rowcount=$(Rscript --slave -e "cat(tryCatch({v<-RSQLite::dbConnect(RSQLite::SQLite(),'${DB_PATH}');on.exit(RSQLite::dbDisconnect(v));RSQLite::dbGetQuery(v,'SELECT COUNT(*) AS n FROM prices')\$n},error=function(e)-1L))" 2>/dev/null)
  echo "[start.sh] prices table row count: ${rowcount}"
  if [ -z "$rowcount" ] || [ "$rowcount" -lt 1 ] 2>/dev/null; then
    echo "[start.sh] DB missing/empty prices table -> rebuild"
    needs_build=1
  fi
fi

if [ "$needs_build" = "1" ]; then
  if [ ! -f "$SEED_CSV" ]; then
    echo "[start.sh] ERROR: seed CSV not found at ${SEED_CSV} — cannot rebuild DB"
  else
    echo "[start.sh] Building DB from seed CSV..."
    mkdir -p "$(dirname "$DB_PATH")"
    export DB_PATH CSV_PATH="$SEED_CSV"
    if Rscript /scripts/build_db.R; then
      echo "[start.sh] DB build OK: $(ls -l "$DB_PATH")"
    else
      echo "[start.sh] ERROR: DB build failed (exit $?)"
    fi
  fi
fi

# ── Auto-analysis: compute pairs if not already done ────────────────────────
# On a fresh volume the pairs table is empty — compute it now so the app
# has data immediately on first deploy. On subsequent restarts the cron
# job keeps it fresh, so we skip to avoid slow startups.
if [ -f "$DB_PATH" ] && [ -s "$DB_PATH" ]; then
  pairs_count=$(Rscript --slave -e "cat(tryCatch({v<-RSQLite::dbConnect(RSQLite::SQLite(),'${DB_PATH}');on.exit(RSQLite::dbDisconnect(v));RSQLite::dbGetQuery(v,'SELECT COUNT(*) AS n FROM pairs')\$n},error=function(e)-1L))" 2>/dev/null)
  echo "[start.sh] pairs table row count: ${pairs_count}"
  if [ -z "$pairs_count" ] || [ "$pairs_count" -lt 1 ] 2>/dev/null; then
    echo "[start.sh] Computing pairs analysis (first run)..."
    Rscript /scripts/compute_analysis.R
    echo "[start.sh] Analysis done."
  fi
fi

# ── Load hourly candles if not already loaded ───────────────────────────────
# On existing volumes the DB may predate the hourly table, so load it separately
# without rebuilding the whole DB.
if [ -f "$DB_PATH" ] && [ -s "$DB_PATH" ]; then
  hourly_count=$(Rscript --slave -e "cat(tryCatch({v<-RSQLite::dbConnect(RSQLite::SQLite(),'${DB_PATH}');on.exit(RSQLite::dbDisconnect(v));RSQLite::dbGetQuery(v,'SELECT COUNT(*) AS n FROM hourly_prices')\$n},error=function(e)-1L))" 2>/dev/null)
  echo "[start.sh] hourly_prices row count: ${hourly_count}"
  if [ -z "$hourly_count" ] || [ "$hourly_count" -lt 1 ] 2>/dev/null; then
    echo "[start.sh] Loading hourly candles..."
    Rscript /scripts/load_hourly.R
    echo "[start.sh] Hourly load done."
  fi
fi

# ── Background daily updater (replaces cron — more reliable in Docker) ─────
# Runs daily_update.R at 06:00 UTC (09:00 MSK) in a background loop.
# All env vars are inherited, logs go to stdout (visible in Railway).
(
  echo "[updater] Background updater started, target: 06:00 UTC daily"
  while true; do
    current_h=$(date -u +%H)
    current_m=$(date -u +%M)
    if [ "$current_h" = "06" ] && [ "$current_m" = "00" ]; then
      echo "[updater] $(date -u) — Running daily_update.R..."
      /usr/local/bin/Rscript /scripts/daily_update.R 2>&1
      echo "[updater] $(date -u) — daily_update.R finished."
      # Sleep 90s to skip past this minute (avoid double-run)
      sleep 90
    fi
    # Check every 30 seconds
    sleep 30
  done
) &
echo "[start.sh] Background updater launched (PID $!)"

# Start Shiny app
PORT="${PORT:-3000}"
echo "[start.sh] Starting Shiny on port ${PORT}"
exec /usr/local/bin/R -e "shiny::runApp('/app', host='0.0.0.0', port=${PORT})"
