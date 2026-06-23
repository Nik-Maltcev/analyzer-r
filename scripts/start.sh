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

# Start cron daemon
cron

# Start Shiny app
PORT="${PORT:-3000}"
echo "[start.sh] Starting Shiny on port ${PORT}"
exec /usr/local/bin/R -e "shiny::runApp('/app', host='0.0.0.0', port=${PORT})"
