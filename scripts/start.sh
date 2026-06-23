#!/bin/bash

# Save env vars for cron
env | grep -E '^[A-Za-z_][A-Za-z_0-9]*=' > /etc/environment || true

DB_PATH="${DB_PATH:-/data/market.db}"

# Railway mounts a fresh volume over /data, hiding the build-time DB.
# Rebuild from the seed CSV (kept at /opt/seed, outside the volume) on first boot.
if [ ! -f "$DB_PATH" ]; then
  echo "[start.sh] Database not found at $DB_PATH — building from seed CSV..."
  mkdir -p "$(dirname "$DB_PATH")"
  export DB_PATH
  Rscript /scripts/build_db.R
  echo "[start.sh] Database ready."
fi

# Start cron daemon
cron

# Start Shiny app
PORT="${PORT:-3000}"
exec /usr/local/bin/R -e "shiny::runApp('/app', host='0.0.0.0', port=${PORT})"
