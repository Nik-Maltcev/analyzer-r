#!/usr/bin/env Rscript
# ── Daily update: fetch latest prices + recompute analysis ───────────────────
# Runs via cron at 06:00 UTC (09:00 MSK) daily
# Requires env vars: TWELVEDATA_API_KEY, DB_PATH (optional)

library(RSQLite)
library(jsonlite)

source("/scripts/tickers.R")

DB_PATH <- Sys.getenv("DB_PATH", "data/market.db")
API_KEY <- Sys.getenv("TWELVEDATA_API_KEY", "")

if (nchar(API_KEY) < 10) stop("Set TWELVEDATA_API_KEY environment variable")
if (!file.exists(DB_PATH)) stop("Database not found. Run init_db.R first.")

con <- dbConnect(SQLite(), DB_PATH)
today <- format(Sys.Date(), "%Y-%m-%d")

cat(sprintf("[%s] === Daily Update Started ===\n", Sys.time()))

# ── Fetch latest data ────────────────────────────────────────────────────────
fetch_batch <- function(symbols, api_key) {
  symbols_str <- paste(symbols, collapse = ",")
  url <- paste0(
    "https://api.twelvedata.com/time_series?",
    "symbol=", URLencode(symbols_str),
    "&interval=1day",
    "&outputsize=5",
    "&apikey=", api_key
  )
  tryCatch(
    fromJSON(url, flatten = TRUE),
    error = function(e) { list(status = "error", message = e$message) }
  )
}

update_market <- function(tickers, market_name, con, api_key) {
  batches <- split(tickers, ceiling(seq_along(tickers) / 8))
  total_new <- 0
  ok_count <- 0
  fail_count <- 0

  for (b_idx in seq_along(batches)) {
    batch <- batches[[b_idx]]
    resp <- fetch_batch(batch, api_key)

    if (length(batch) == 1) {
      resp <- list(resp)
      names(resp) <- batch[1]
    }

    for (sym in names(resp)) {
      d <- resp[[sym]]
      if (is.null(d$values) || !is.data.frame(d$values) || nrow(d$values) == 0) {
        fail_count <- fail_count + 1
        next
      }
      vals <- d$values
      n_rows <- nrow(vals)
      vol <- if (!is.null(vals$volume)) as.numeric(vals$volume) else rep(NA_real_, n_rows)
      if (length(vol) != n_rows) vol <- rep(NA_real_, n_rows)
      df <- data.frame(
        ticker = rep(sym, n_rows),
        date   = vals$datetime,
        close  = as.numeric(vals$close),
        volume = vol,
        market = rep(market_name, n_rows),
        stringsAsFactors = FALSE
      )
      existing <- dbGetQuery(con,
        sprintf("SELECT date FROM prices WHERE ticker = '%s' ORDER BY date DESC LIMIT 10", sym))
      new_rows <- df[!df$date %in% existing$date, ]
      if (nrow(new_rows) > 0) {
        dbWriteTable(con, "prices", new_rows, append = TRUE, row.names = FALSE)
        total_new <- total_new + nrow(new_rows)
      }
      ok_count <- ok_count + 1
    }

    if (b_idx < length(batches)) Sys.sleep(75)
  }

  cat(sprintf("  [%s] %d OK, %d fail, %d new rows\n", market_name, ok_count, fail_count, total_new))

  dbExecute(con, "
    INSERT INTO update_log (market, tickers_ok, tickers_fail, rows_added, status, message)
    VALUES (?, ?, ?, ?, 'success', ?)",
    params = list(market_name, ok_count, fail_count, total_new,
                  sprintf("Daily update: %d new rows", total_new)))

  total_new
}

cat("Updating prices...\n")
n1 <- update_market(CRYPTO_TICKERS, "crypto", con, API_KEY)
n2 <- update_market(STOCK_TICKERS, "stocks", con, API_KEY)
n3 <- update_market(FOREX_TICKERS, "forex", con, API_KEY)
cat(sprintf("Total new rows: %d\n\n", n1 + n2 + n3))

dbDisconnect(con)

# ── Recompute pairs analysis (all markets) ──────────────────────────────────
cat("Recomputing pairs analysis...\n")
system("Rscript /scripts/compute_analysis.R")

cat(sprintf("\n[%s] === Daily Update Complete ===\n", Sys.time()))
