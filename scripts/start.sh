#!/bin/bash
set -e

# Pass environment variables to cron
printenv | grep -v "no_proxy" >> /etc/environment

# Start cron daemon in background
cron

# Initialize DB if not exists
if [ ! -f /data/market.db ]; then
  echo "First run: initializing database..."
  cd /scripts && Rscript init_db.R
fi

# Start Shiny app
PORT="${PORT:-3000}"
exec R -e "shiny::runApp('/app', host='0.0.0.0', port=${PORT})"
