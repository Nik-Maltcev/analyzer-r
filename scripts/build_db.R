#!/usr/bin/env Rscript
# Build SQLite database from seed CSV.
# Runs at Docker build time (bakes DB into image for plain Docker)
# AND at runtime via start.sh when /data is a fresh volume (Railway).
library(RSQLite)

CSV_PATH <- Sys.getenv("CSV_PATH", "/opt/seed/all_markets_3yr.csv")
DB_PATH  <- Sys.getenv("DB_PATH",  "/data/market.db")

if (!file.exists(CSV_PATH)) stop(sprintf("Seed CSV not found: %s", CSV_PATH))
dir.create(dirname(DB_PATH), recursive = TRUE, showWarnings = FALSE)

cat(sprintf("Building database from CSV...\n  CSV: %s\n  DB:  %s\n", CSV_PATH, DB_PATH))
df <- read.csv(CSV_PATH, stringsAsFactors = FALSE)
df <- df[!duplicated(df[, c("ticker", "date")]), ]
cat(sprintf("  Rows: %d, Tickers: %d\n", nrow(df), length(unique(df$ticker))))

con <- dbConnect(SQLite(), DB_PATH)

dbExecute(con, "
  CREATE TABLE IF NOT EXISTS prices (
    ticker TEXT NOT NULL,
    date   TEXT NOT NULL,
    close  REAL NOT NULL,
    volume REAL,
    market TEXT,
    PRIMARY KEY (ticker, date)
  )
")

dbExecute(con, "
  CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT, ticker_a TEXT, ticker_b TEXT,
    z_score REAL, z_forecast REAL, signal TEXT,
    strength TEXT, is_coint INTEGER, corr REAL,
    created_at TEXT DEFAULT (datetime('now'))
  )
")

dbExecute(con, "
  CREATE TABLE IF NOT EXISTS update_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT (datetime('now')),
    market TEXT, tickers_ok INTEGER, tickers_fail INTEGER,
    rows_added INTEGER, status TEXT, message TEXT
  )
")

dbExecute(con, "
  CREATE TABLE IF NOT EXISTS pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market      TEXT NOT NULL,
    ticker_a    TEXT NOT NULL,
    ticker_b    TEXT NOT NULL,
    corr        REAL,
    halflife    INTEGER,
    t_stat      REAL,
    is_coint    INTEGER,
    hedge_ratio REAL,
    score       REAL,
    z_now       REAL,
    z_forecast  REAL,
    signal      TEXT,
    signal_type TEXT,
    strength    TEXT,
    computed_at TEXT DEFAULT (datetime('now')),
    UNIQUE (market, ticker_a, ticker_b)
  )
")

dbExecute(con, "CREATE INDEX IF NOT EXISTS idx_pairs_market ON pairs(market)")
dbExecute(con, "CREATE INDEX IF NOT EXISTS idx_pairs_score   ON pairs(score DESC)")

dbWriteTable(con, "prices", df, append = TRUE, row.names = FALSE)

dbExecute(con, "CREATE INDEX IF NOT EXISTS idx_prices_ticker ON prices(ticker)")
dbExecute(con, "CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(date)")

dbExecute(con, "INSERT INTO update_log (market, tickers_ok, rows_added, status, message)
  VALUES ('all', ?, ?, 'success', 'Initial build from CSV')",
  params = list(length(unique(df$ticker)), nrow(df)))

dbDisconnect(con)

cat(sprintf("  Database ready: %s (%.1f MB)\n", DB_PATH,
            file.info(DB_PATH)$size / 1024^2))
