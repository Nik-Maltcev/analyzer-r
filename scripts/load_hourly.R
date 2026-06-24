#!/usr/bin/env Rscript
# ── Load hourly candles into existing DB (without rebuild) ──────────────────
# Called by start.sh if hourly_prices table is missing or empty.
# Only loads the CSV, doesn't touch prices/pairs/signals tables.
library(RSQLite)

DB_PATH      <- Sys.getenv("DB_PATH", "/data/market.db")
HOURLY_PATH  <- Sys.getenv("HOURLY_PATH", "/opt/seed/hourly_6coins_2yr.csv")

if (!file.exists(DB_PATH)) stop("DB not found: ", DB_PATH)
if (!file.exists(HOURLY_PATH)) {
  cat("Hourly CSV not found:", HOURLY_PATH, "— skipping\n")
  quit("no", 0)
}

con <- dbConnect(SQLite(), DB_PATH)
on.exit(dbDisconnect(con))

# Check if table exists and has data
tables <- dbGetQuery(con, "SELECT name FROM sqlite_master WHERE type='table' AND name='hourly_prices'")$name
if ("hourly_prices" %in% tables) {
  n <- dbGetQuery(con, "SELECT COUNT(*) AS n FROM hourly_prices")$n
  if (n > 0) {
    cat(sprintf("hourly_prices already has %d rows, skipping\n", n))
    quit("no", 0)
  }
}

cat("Loading hourly candles from:", HOURLY_PATH, "\n")
df <- read.csv(HOURLY_PATH, stringsAsFactors = FALSE)
df <- df[!duplicated(df[, c("ticker", "timestamp")]), ]
df <- df[, c("ticker", "timestamp", "date", "hour", "open", "high", "low", "close", "volume")]

dbExecute(con, "
  CREATE TABLE IF NOT EXISTS hourly_prices (
    ticker    TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    date      TEXT NOT NULL,
    hour      INTEGER NOT NULL,
    open      REAL,
    high      REAL,
    low       REAL,
    close     REAL NOT NULL,
    volume    REAL,
    PRIMARY KEY (ticker, timestamp)
  )
")

dbWriteTable(con, "hourly_prices", df, append = TRUE, row.names = FALSE)
dbExecute(con, "CREATE INDEX IF NOT EXISTS idx_hourly_ticker ON hourly_prices(ticker)")
dbExecute(con, "CREATE INDEX IF NOT EXISTS idx_hourly_hour   ON hourly_prices(hour)")

cat(sprintf("Loaded %d hourly rows, %d tickers\n", nrow(df), length(unique(df$ticker))))
