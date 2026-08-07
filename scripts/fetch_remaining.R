#!/usr/bin/env Rscript
# Fetch stocks + forex (crypto already done)
library(jsonlite)

API_KEY <- "54ebd565c7e84e3587a8db4331e16b1d"
START_DATE <- format(Sys.Date() - 365 * 3, "%Y-%m-%d")
END_DATE <- format(Sys.Date(), "%Y-%m-%d")
OUT_DIR <- "d:/Crypto-Analyzer-R/data"

source("d:/Crypto-Analyzer-R/scripts/tickers.R")

fetch_one <- function(symbols, api_key) {
  symbols_str <- paste(symbols, collapse = ",")
  url <- paste0(
    "https://api.twelvedata.com/time_series?",
    "symbol=", URLencode(symbols_str),
    "&interval=1day&start_date=", START_DATE,
    "&end_date=", END_DATE,
    "&outputsize=5000&apikey=", api_key
  )
  tryCatch(fromJSON(url, flatten = TRUE),
           error = function(e) { cat("  ERR:", e$message, "\n"); NULL })
}

fetch_market <- function(tickers, market_name) {
  batches <- split(tickers, ceiling(seq_along(tickers) / 8))
  all_data <- data.frame()
  cat(sprintf("\n=== %s: %d tickers, %d batches ===\n", toupper(market_name), length(tickers), length(batches)))

  for (b_idx in seq_along(batches)) {
    batch <- batches[[b_idx]]
    cat(sprintf("  [%d/%d] %s ...", b_idx, length(batches), paste(batch[1:min(3,length(batch))], collapse=",")))
    resp <- fetch_one(batch, API_KEY)
    if (is.null(resp)) { cat(" FAILED\n"); next }
    if (length(batch) == 1) { resp <- list(resp); names(resp) <- batch[1] }

    batch_rows <- 0
    for (sym in names(resp)) {
      d <- resp[[sym]]
      if (is.null(d$values) || !is.data.frame(d$values) || nrow(d$values) == 0) next
      vals <- d$values; n <- nrow(vals)
      vol <- tryCatch(as.numeric(vals$volume), error = function(e) rep(NA, n))
      if (length(vol) != n) vol <- rep(NA, n)
      df <- data.frame(ticker=sym, date=vals$datetime, close=as.numeric(vals$close),
                       volume=vol, market=market_name, stringsAsFactors=FALSE)
      all_data <- rbind(all_data, df)
      batch_rows <- batch_rows + n
    }
    cat(sprintf(" OK (%d rows)\n", batch_rows))

    if (b_idx < length(batches)) {
      cat("    waiting 75s...\n")
      Sys.sleep(75)
    }
  }
  cat(sprintf("  DONE: %d rows, %d tickers\n", nrow(all_data), length(unique(all_data$ticker))))
  all_data
}

stocks_data <- fetch_market(STOCK_TICKERS, "stocks")
forex_data  <- fetch_market(FOREX_TICKERS, "forex")

# Append to existing CSV
existing_file <- file.path(OUT_DIR, "all_markets_3yr.csv")
if (file.exists(existing_file)) {
  existing <- read.csv(existing_file, stringsAsFactors = FALSE)
  combined <- rbind(existing, stocks_data, forex_data)
} else {
  combined <- rbind(stocks_data, forex_data)
}
combined <- combined[!is.na(combined$close) & combined$close > 0, ]

write.csv(combined, existing_file, row.names = FALSE)
cat(sprintf("\nSaved: %s rows, %d tickers -> %s\n",
            format(nrow(combined), big.mark=","), length(unique(combined$ticker)), existing_file))
