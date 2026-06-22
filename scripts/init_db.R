#!/usr/bin/env Rscript
# ── Initial DB setup: create SQLite + load 3 years of data ───────────────────
# Run once: Rscript scripts/init_db.R
# Requires env var: TWELVEDATA_API_KEY

library(RSQLite)
library(jsonlite)

source("scripts/tickers.R")

DB_PATH <- Sys.getenv("DB_PATH", "data/market.db")
API_KEY <- Sys.getenv("TWELVEDATA_API_KEY", "")

if (nchar(API_KEY) < 10) stop("Set TWELVEDATA_API_KEY environment variable")

# Create data directory
dir.create(dirname(DB_PATH), recursive = TRUE, showWarnings = FALSE)

# ── Create DB schema ─────────────────────────────────────────────────────────
con <- dbConnect(SQLite(), DB_PATH)

dbExecute(con, "
  CREATE TABLE IF NOT EXISTS prices (
    ticker TEXT NOT NULL,
    date   TEXT NOT NULL,
    open   REAL,
    high   REAL,
    low    REAL,
    close  REAL NOT NULL,
    volume REAL,
    market TEXT NOT NULL,
    PRIMARY KEY (ticker, date)
  )
")

dbExecute(con, "
  CREATE TABLE IF NOT EXISTS signals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    date       TEXT NOT NULL,
    ticker_a   TEXT NOT NULL,
    ticker_b   TEXT NOT NULL,
    z_score    REAL,
    z_forecast REAL,
    signal     TEXT,
    strength   TEXT,
    is_coint   INTEGER,
    corr       REAL,
    created_at TEXT DEFAULT (datetime('now'))
  )
")

dbExecute(con, "
  CREATE TABLE IF NOT EXISTS update_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  TEXT DEFAULT (datetime('now')),
    market     TEXT,
    tickers_ok INTEGER,
    tickers_fail INTEGER,
    rows_added INTEGER,
    status     TEXT,
    message    TEXT
  )
")

dbExecute(con, "CREATE INDEX IF NOT EXISTS idx_prices_ticker ON prices(ticker)")
dbExecute(con, "CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(date)")
dbExecute(con, "CREATE INDEX IF NOT EXISTS idx_signals_date ON signals(date)")

cat("Database schema created:", DB_PATH, "\n")

# ── Fetch function ───────────────────────────────────────────────────────────
fetch_batch <- function(symbols, start_date, end_date, api_key) {
  symbols_str <- paste(symbols, collapse = ",")
  url <- paste0(
    "https://api.twelvedata.com/time_series?",
    "symbol=", URLencode(symbols_str),
    "&interval=1day",
    "&start_date=", start_date,
    "&end_date=", end_date,
    "&outputsize=5000",
    "&apikey=", api_key
  )
  tryCatch(
    fromJSON(url, flatten = TRUE),
    error = function(e) { list(status = "error", message = e$message) }
  )
}

# ── Load data for a market ───────────────────────────────────────────────────
load_market <- function(tickers, market_name, con, api_key) {
  start_date <- format(Sys.Date() - 365 * 3, "%Y-%m-%d")
  end_date   <- format(Sys.Date(), "%Y-%m-%d")

  batches <- split(tickers, ceiling(seq_along(tickers) / 8))
  total_rows <- 0
  ok_count <- 0
  fail_count <- 0

  cat(sprintf("\n[%s] Loading %d tickers in %d batches...\n",
              toupper(market_name), length(tickers), length(batches)))

  for (b_idx in seq_along(batches)) {
    batch <- batches[[b_idx]]
    cat(sprintf("  Batch %d/%d: %s\n", b_idx, length(batches), paste(batch, collapse = ", ")))

    resp <- fetch_batch(batch, start_date, end_date, api_key)

    # Handle single symbol response
    if (length(batch) == 1) {
      resp <- list(resp)
      names(resp) <- batch[1]
    }

    for (sym in names(resp)) {
      d <- resp[[sym]]
      if (is.null(d$values) || !is.data.frame(d$values) || nrow(d$values) == 0) {
        cat(sprintf("    SKIP: %s (no data)\n", sym))
        fail_count <- fail_count + 1
        next
      }
      vals <- d$values
      df <- data.frame(
        ticker = sym,
        date   = vals$datetime,
        open   = as.numeric(vals$open),
        high   = as.numeric(vals$high),
        low    = as.numeric(vals$low),
        close  = as.numeric(vals$close),
        volume = as.numeric(vals$volume),
        market = market_name,
        stringsAsFactors = FALSE
      )
      # Upsert
      dbExecute(con, "DELETE FROM prices WHERE ticker = ? AND date IN (?)",
                params = list(sym, paste(df$date, collapse = "','")))
      dbWriteTable(con, "prices", df, append = TRUE, row.names = FALSE)
      total_rows <- total_rows + nrow(df)
      ok_count <- ok_count + 1
      cat(sprintf("    OK: %s (%d rows)\n", sym, nrow(df)))
    }

    # Rate limit: 8 requests/min on free tier
    if (b_idx < length(batches)) {
      cat("    Waiting 10s (rate limit)...\n")
      Sys.sleep(10)
    }
  }

  # Log
  dbExecute(con, "
    INSERT INTO update_log (market, tickers_ok, tickers_fail, rows_added, status, message)
    VALUES (?, ?, ?, ?, 'success', ?)",
    params = list(market_name, ok_count, fail_count, total_rows,
                  sprintf("Initial load: %d OK, %d failed", ok_count, fail_count)))

  cat(sprintf("[%s] Done: %d tickers OK, %d failed, %d total rows\n\n",
              toupper(market_name), ok_count, fail_count, total_rows))
}

# ── Run initial load ─────────────────────────────────────────────────────────
cat("=== CryptoScope Initial Data Load ===\n")
cat(sprintf("Date range: %s to %s\n", format(Sys.Date() - 365*3, "%Y-%m-%d"), format(Sys.Date(), "%Y-%m-%d")))
cat(sprintf("Total tickers: %d (Crypto: %d, Stocks: %d, Forex: %d)\n",
            length(CRYPTO_TICKERS) + length(STOCK_TICKERS) + length(FOREX_TICKERS),
            length(CRYPTO_TICKERS), length(STOCK_TICKERS), length(FOREX_TICKERS)))
cat("====================================\n")

load_market(CRYPTO_TICKERS, "crypto", con, API_KEY)
load_market(STOCK_TICKERS, "stocks", con, API_KEY)
load_market(FOREX_TICKERS, "forex", con, API_KEY)

# Summary
total <- dbGetQuery(con, "SELECT COUNT(*) as n FROM prices")$n
tickers_loaded <- dbGetQuery(con, "SELECT COUNT(DISTINCT ticker) as n FROM prices")$n
cat(sprintf("\n=== COMPLETE ===\nTotal: %s rows, %d tickers loaded\nDB: %s\n",
            format(total, big.mark = ","), tickers_loaded, DB_PATH))

dbDisconnect(con)
