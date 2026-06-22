#!/bin/bash
set -e

# Export all env vars so child processes (cron, Rscript) can see them
export $(env | xargs)

# Pass environment variables to cron
env >> /etc/environment

# Start cron daemon in background
cron

# Initialize DB if not exists
if [ ! -f /data/market.db ]; then
  echo "First run: initializing database..."
  cd /scripts && /usr/local/bin/Rscript /scripts/init_db.R
fi

# Start Shiny app
PORT="${PORT:-3000}"
exec /usr/local/bin/R -e "shiny::runApp('/app', host='0.0.0.0', port=${PORT})"
