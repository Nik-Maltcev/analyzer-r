FROM rocker/r-ver:4.4.0

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libcurl4-openssl-dev \
    libssl-dev \
    libxml2-dev \
    libfontconfig1-dev \
    libfreetype6-dev \
    libpng-dev \
    libtiff5-dev \
    libjpeg-dev \
    libharfbuzz-dev \
    libfribidi-dev \
    libsqlite3-dev \
    cron \
    && rm -rf /var/lib/apt/lists/*

# Install R packages
RUN install2.r --error --skipinstalled \
    shiny \
    bslib \
    dplyr \
    tidyr \
    ggplot2 \
    zoo \
    DT \
    corrplot \
    tibble \
    lubridate \
    RSQLite \
    jsonlite

# Create directories. /opt/seed holds the CSV outside the /data volume mount.
RUN mkdir -p /app /scripts /data /opt/seed

# Copy seed CSV to /opt/seed (NOT /data, which Railway masks with a volume)
COPY data/all_markets_3yr.csv /opt/seed/all_markets_3yr.csv
COPY scripts/ /scripts/

# Build DB at image build time (works for plain Docker without a volume).
# On Railway the /data volume masks this DB; start.sh rebuilds it on first boot.
RUN Rscript /scripts/build_db.R

# Copy app
COPY artifacts/crypto-analyzer/ /app/

# Setup cron job: daily at 06:00 UTC (09:00 MSK)
RUN echo "0 6 * * * /usr/local/bin/Rscript /scripts/daily_update.R >> /data/cron.log 2>&1" > /etc/cron.d/daily-update \
    && chmod 0644 /etc/cron.d/daily-update \
    && crontab /etc/cron.d/daily-update

# Startup script
COPY scripts/start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 3000

CMD ["/start.sh"]
