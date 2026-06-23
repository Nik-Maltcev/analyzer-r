#!/bin/bash
set -e

# Save env vars for cron (filter only valid variable names)
env | grep -E '^[A-Za-z_][A-Za-z_0-9]*=' > /etc/environment || true

# Start cron daemon in background
cron

# Initialize DB if not exists
if [ ! -f /data/market.db ]; then
  echo "First run: initializing database..."
  /usr/local/bin/Rscript /scripts/init_db.R
fi

# Start Shiny app
PORT="${PORT:-3000}"
exec /usr/local/bin/R -e "shiny::runApp('/app', host='0.0.0.0', port=${PORT})"
