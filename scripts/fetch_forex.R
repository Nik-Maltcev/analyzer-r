#!/usr/bin/env Rscript
library(jsonlite)

API_KEY <- "54ebd565c7e84e3587a8db4331e16b1d"
START_DATE <- format(Sys.Date() - 365 * 3, "%Y-%m-%d")
END_DATE <- format(Sys.Date(), "%Y-%m-%d")

FOREX <- c(
  "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "AUD/USD",
  "USD/CAD", "NZD/USD", "EUR/GBP", "EUR/JPY", "GBP/JPY",
  "AUD/JPY", "EUR/CHF", "EUR/AUD", "GBP/AUD", "EUR/CAD",
  "AUD/NZD", "GBP/CAD", "USD/MXN", "USD/TRY", "USD/ZAR",
  "USD/SGD", "USD/HKD", "USD/NOK", "USD/SEK", "USD/DKK",
  "EUR/NOK", "EUR/SEK", "EUR/PLN", "USD/CNH", "USD/INR"
)

batches <- split(FOREX, ceiling(seq_along(FOREX) / 8))
all_data <- data.frame()

cat(sprintf("Fetching %d forex tickers in %d batches\n", length(FOREX), length(batches)))

for (b_idx in seq_along(batches)) {
  batch <- batches[[b_idx]]
  cat(sprintf("  [%d/%d] %s ...", b_idx, length(batches), paste(batch[1:3], collapse=",")))

  url <- paste0("https://api.twelvedata.com/time_series?",
    "symbol=", URLencode(paste(batch, collapse=",")),
    "&interval=1day&start_date=", START_DATE,
    "&end_date=", END_DATE, "&outputsize=5000&apikey=", API_KEY)

  resp <- tryCatch(fromJSON(url, flatten=TRUE), error=function(e) { cat(" ERR:", e$message, "\n"); NULL })
  if (is.null(resp)) next
  if (length(batch) == 1) { resp <- list(resp); names(resp) <- batch[1] }

  rows <- 0
  for (sym in names(resp)) {
    d <- resp[[sym]]
    if (is.null(d$values) || !is.data.frame(d$values) || nrow(d$values) == 0) next
    vals <- d$values; n <- nrow(vals)
    vol <- tryCatch(as.numeric(vals$volume), error=function(e) rep(NA, n))
    if (length(vol) != n) vol <- rep(NA, n)
    all_data <- rbind(all_data, data.frame(
      ticker=sym, date=vals$datetime, close=as.numeric(vals$close),
      volume=vol, market="forex", stringsAsFactors=FALSE))
    rows <- rows + n
  }
  cat(sprintf(" OK (%d rows)\n", rows))

  if (b_idx < length(batches)) { cat("    waiting 75s...\n"); Sys.sleep(75) }
}

cat(sprintf("\nForex done: %d rows, %d tickers\n", nrow(all_data), length(unique(all_data$ticker))))

# Append to existing CSV
csv_path <- "d:/Crypto-Analyzer-R/data/all_markets_3yr.csv"
existing <- read.csv(csv_path, stringsAsFactors=FALSE)
combined <- rbind(existing, all_data)
combined <- combined[!duplicated(combined[,c("ticker","date")]), ]
write.csv(combined, csv_path, row.names=FALSE)
cat(sprintf("Saved: %d total rows, %d tickers\n", nrow(combined), length(unique(combined$ticker))))
