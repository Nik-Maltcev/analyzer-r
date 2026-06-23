#!/bin/bash
set -e

# Debug: show if key is set
echo "TWELVEDATA_API_KEY is set: $(if [ -n "$TWELVEDATA_API_KEY" ]; then echo YES; else echo NO; fi)"

# Save env vars for cron
env | grep -E '^[A-Za-z_][A-Za-z_0-9]*=' > /etc/environment || true

# Start cron daemon in background
cron

# Initialize DB if not exists AND api key is available
if [ ! -f /data/market.db ] && [ -n "$TWELVEDATA_API_KEY" ]; then
  echo "First run: initializing database..."
  /usr/local/bin/Rscript /scripts/init_db.R
elif [ ! -f /data/market.db ]; then
  echo "WARNING: No TWELVEDATA_API_KEY set. Skipping DB init. Use the app UI to load data."
fi

# Start Shiny app
PORT="${PORT:-3000}"
exec /usr/local/bin/R -e "shiny::runApp('/app', host='0.0.0.0', port=${PORT})"
