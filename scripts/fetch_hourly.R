#!/usr/bin/env Rscript
# ── Fetch 2 years of hourly candles from Binance for 6 coins ────────────────
# No API key needed. 1000 candles/request, ~18 requests per coin, ~108 total.
# Saves to data/hourly_6coins_2yr.csv
# Run: Rscript scripts/fetch_hourly.R

library(jsonlite)

COINS <- list(
  list(symbol = "BTCUSDT", name = "BTC/USD"),
  list(symbol = "ETHUSDT", name = "ETH/USD"),
  list(symbol = "BNBUSDT", name = "BNB/USD"),
  list(symbol = "SOLUSDT", name = "SOL/USD"),
  list(symbol = "XRPUSDT", name = "XRP/USD"),
  list(symbol = "DOGEUSDT", name = "DOGE/USD")
)

OUT_DIR <- "d:/Crypto-Analyzer-R/data"
dir.create(OUT_DIR, showWarnings = FALSE, recursive = TRUE)
OUT_FILE <- file.path(OUT_DIR, "hourly_6coins_2yr.csv")

# 2 years ago in milliseconds
end_time   <- as.numeric(as.POSIXct(Sys.Date())) * 1000
start_time <- as.numeric(as.POSIXct(Sys.Date() - 365 * 2)) * 1000

cat("=== Binance Hourly Fetch ===\n")
cat(sprintf("Date range: %s to %s (2 years)\n",
            format(as.POSIXct(start_time / 1000, origin = "1970-01-01"), "%Y-%m-%d"),
            format(as.POSIXct(end_time / 1000, origin = "1970-01-01"), "%Y-%m-%d")))
cat(sprintf("Coins: %d, ~18 requests each\n\n", length(COINS)))

fetch_batch <- function(symbol, start_ms, end_ms) {
  url <- paste0(
    "https://api.binance.com/api/v3/klines?",
    "symbol=", symbol,
    "&interval=1h",
    "&limit=1000",
    "&startTime=", start_ms,
    "&endTime=", end_ms
  )
  # Retry up to 3 times with 5s pause (Binance sometimes times out)
  for (attempt in 1:3) {
    r <- tryCatch({
      fromJSON(url, simplifyVector = TRUE)
    }, error = function(e) {
      if (attempt < 3) { cat(sprintf("  retry %d/3...\n", attempt)); Sys.sleep(5) }
      NULL
    })
    if (!is.null(r)) {
      if (!is.matrix(r) && !is.data.frame(r)) {
        cat("  API error:", paste(unlist(r), collapse = " "), "\n")
        return(NULL)
      }
      return(r)
    }
  }
  cat("  Failed after 3 retries\n")
  NULL
}

all_data <- data.frame()

for (coin in COINS) {
  cat(sprintf("[%s] Fetching %s...\n", coin$symbol, coin$name))
  coin_data <- data.frame()
  cur_start <- start_time

  batch <- 1
  while (cur_start < end_time) {
    resp <- fetch_batch(coin$symbol, cur_start, end_time)
    if (is.null(resp) || length(resp) == 0) {
      cat(sprintf("  Batch %d: empty response, stopping\n", batch))
      break
    }

    # Binance returns matrix: [openTime, open, high, low, close, volume, closeTime, ...]
    # Timestamps come as strings in scientific notation, need as.character first
    open_time <- as.numeric(as.character(resp[, 1]))
    df <- data.frame(
      ticker   = coin$name,
      timestamp = as.POSIXct(open_time / 1000, origin = "1970-01-01", tz = "UTC"),
      date     = as.Date(as.POSIXct(open_time / 1000, origin = "1970-01-01", tz = "UTC")),
      hour     = as.integer(format(as.POSIXct(open_time / 1000, origin = "1970-01-01", tz = "UTC"), "%H")),
      open     = as.numeric(resp[, 2]),
      high     = as.numeric(resp[, 3]),
      low      = as.numeric(resp[, 4]),
      close    = as.numeric(resp[, 5]),
      volume   = as.numeric(resp[, 6]),
      stringsAsFactors = FALSE
    )
    coin_data <- rbind(coin_data, df)

    # Next batch starts after last candle
    last_open_time <- open_time[length(open_time)]
    cur_start <- last_open_time + 1  # +1ms to avoid duplicate

    cat(sprintf("  Batch %d: %d candles (total %d)\n", batch, nrow(df), nrow(coin_data)))

    batch <- batch + 1
    # Safety: 25 batches max (25,000 candles)
    if (batch > 25) break

    # Small pause to be nice to Binance
    Sys.sleep(0.2)
  }

  # Deduplicate
  coin_data <- coin_data[!duplicated(coin_data$timestamp), ]
  cat(sprintf("[%s] Done: %d candles, %s to %s\n\n",
              coin$symbol, nrow(coin_data),
              format(min(coin_data$timestamp), "%Y-%m-%d %H:00"),
              format(max(coin_data$timestamp), "%Y-%m-%d %H:00")))

  all_data <- rbind(all_data, coin_data)
}

# Save
all_data <- all_data[order(all_data$ticker, all_data$timestamp), ]
write.csv(all_data, OUT_FILE, row.names = FALSE)

cat("=== DONE ===\n")
cat(sprintf("Total: %s rows, %d coins\n",
            format(nrow(all_data), big.mark = ","),
            length(unique(all_data$ticker))))
cat(sprintf("Saved: %s\n", OUT_FILE))
cat(sprintf("Size: %.1f MB\n", file.info(OUT_FILE)$size / 1024^2))
