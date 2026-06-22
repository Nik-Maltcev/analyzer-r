FROM rocker/r-ver:4.4.0

# System dependencies for R packages
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
    lubridate

# Copy app
COPY artifacts/crypto-analyzer /app

# Expose port (Railway sets PORT env var)
EXPOSE 3000

# Run Shiny app
CMD ["R", "-e", "port <- as.integer(Sys.getenv('PORT', unset='3000')); shiny::runApp('/app', host='0.0.0.0', port=port)"]
