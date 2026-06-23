#!/bin/bash

# Save env vars for cron
env | grep -E '^[A-Za-z_][A-Za-z_0-9]*=' > /etc/environment || true

# Start cron daemon
cron

# Start Shiny app
PORT="${PORT:-3000}"
exec /usr/local/bin/R -e "shiny::runApp('/app', host='0.0.0.0', port=${PORT})"
