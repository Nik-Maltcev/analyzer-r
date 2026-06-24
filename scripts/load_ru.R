#!/usr/bin/env Rscript
# ── Load Russian stocks into existing DB (without rebuild) ──────────────────
# Called by start.sh if 'ru' market is missing from prices table.
# Only loads the CSV, doesn't touch other data.
library(RSQLite)

DB_PATH <- Sys.getenv("DB_PATH", "/data/market.db")
RU_CSV  <- Sys.getenv("RU_CSV_PATH", "/opt/seed/tinkoff_ru_2yr.csv")

if (!file.exists(DB_PATH)) stop("DB not found: ", DB_PATH)
if (!file.exists(RU_CSV)) { cat("RU CSV not found:", RU_CSV, "— skipping\n"); quit("no", 0) }

con <- dbConnect(SQLite(), DB_PATH)
on.exit(dbDisconnect(con))

# Check if ru data already exists
n <- tryCatch(
  dbGetQuery(con, "SELECT COUNT(*) AS n FROM prices WHERE market = 'ru'")$n,
  error = function(e) 0
)
if (n > 0) {
  cat(sprintf("RU market already has %d rows, skipping\n", n))
  quit("no", 0)
}

cat("Loading RU stocks from:", RU_CSV, "\n")
df <- read.csv(RU_CSV, stringsAsFactors = FALSE)
df <- df[!duplicated(df[, c("ticker", "date")]), ]
df <- df[, c("ticker", "date", "close", "volume", "market")]
df <- df[!is.na(df$close) & df$close > 0, ]

# Ensure prices table exists
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

dbWriteTable(con, "prices", df, append = TRUE, row.names = FALSE)
dbExecute(con, "CREATE INDEX IF NOT EXISTS idx_prices_ticker ON prices(ticker)")

cat(sprintf("Loaded %d RU rows, %d tickers\n", nrow(df), length(unique(df$ticker))))
