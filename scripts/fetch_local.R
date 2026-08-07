#!/usr/bin/env Rscript
# Fetch all 180 tickers locally and save to CSV
library(jsonlite)

API_KEY <- "54ebd565c7e84e3587a8db4331e16b1d"
START_DATE <- format(Sys.Date() - 365 * 3, "%Y-%m-%d")
END_DATE <- format(Sys.Date(), "%Y-%m-%d")
OUT_DIR <- "d:/Crypto-Analyzer-R/data"
dir.create(OUT_DIR, showWarnings = FALSE, recursive = TRUE)

source("d:/Crypto-Analyzer-R/scripts/tickers.R")

fetch_batch <- function(symbols, api_key) {
  symbols_str <- paste(symbols, collapse = ",")
  url <- paste0(
    "https://api.twelvedata.com/time_series?",
    "symbol=", URLencode(symbols_str),
    "&interval=1day",
    "&start_date=", START_DATE,
    "&end_date=", END_DATE,
    "&outputsize=5000",
    "&apikey=", api_key
  )
  tryCatch(fromJSON(url, flatten = TRUE),
           error = function(e) { cat("  ERROR:", e$message, "\n"); NULL })
}

fetch_market <- function(tickers, market_name) {
  batches <- split(tickers, ceiling(seq_along(tickers) / 8))
  all_data <- data.frame()

  cat(sprintf("\n=== %s: %d tickers, %d batches ===\n", toupper(market_name), length(tickers), length(batches)))

  for (b_idx in seq_along(batches)) {
    batch <- batches[[b_idx]]
    cat(sprintf("  [%d/%d] %s ...", b_idx, length(batches), paste(batch[1:min(3,length(batch))], collapse=",")))

    resp <- fetch_batch(batch, API_KEY)
    if (is.null(resp)) { cat(" FAILED\n"); next }

    # Single ticker response
    if (length(batch) == 1) {
      resp <- list(resp)
      names(resp) <- batch[1]
    }

    batch_rows <- 0
    for (sym in names(resp)) {
      d <- resp[[sym]]
      if (is.null(d$values) || !is.data.frame(d$values) || nrow(d$values) == 0) next
      vals <- d$values
      n <- nrow(vals)
      vol <- tryCatch(as.numeric(vals$volume), error = function(e) rep(NA, n))
      if (length(vol) != n) vol <- rep(NA, n)
      df <- data.frame(
        ticker = sym,
        date = vals$datetime,
        close = as.numeric(vals$close),
        volume = vol,
        market = market_name,
        stringsAsFactors = FALSE
      )
      all_data <- rbind(all_data, df)
      batch_rows <- batch_rows + n
    }
    cat(sprintf(" OK (%d rows)\n", batch_rows))

    # Rate limit pause
    if (b_idx < length(batches)) {
      cat("    waiting 62s (rate limit: 8 credits/min)...\n")
      Sys.sleep(62)
    }
  }

  cat(sprintf("  [%s] Total: %d rows, %d tickers\n", market_name,
              nrow(all_data), length(unique(all_data$ticker))))
  all_data
}

# Fetch all markets
crypto_data <- fetch_market(CRYPTO_TICKERS, "crypto")
stocks_data <- fetch_market(STOCK_TICKERS, "stocks")
forex_data  <- fetch_market(FOREX_TICKERS, "forex")

# Combine and save
all_data <- rbind(crypto_data, stocks_data, forex_data)
all_data <- all_data[!is.na(all_data$close) & all_data$close > 0, ]

out_file <- file.path(OUT_DIR, "all_markets_3yr.csv")
write.csv(all_data, out_file, row.names = FALSE)

cat(sprintf("\n=== DONE ===\nTotal: %s rows, %d tickers\nSaved: %s\n",
            format(nrow(all_data), big.mark = ","), length(unique(all_data$ticker)), out_file))
