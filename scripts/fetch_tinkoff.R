#!/usr/bin/env Rscript
# ── Fetch 2 years of daily candles for Russian stocks from MOEX ISS API ────
# Free, no auth needed, REST JSON. Paginated (100 rows per page).
# Output: data/tinkoff_ru_2yr.csv
# Run: Rscript scripts/fetch_tinkoff.R

library(jsonlite)

RU_STOCKS <- c(
  "SBER", "GAZP", "LKOH", "GMKN", "ROSN", "VTBR",
  "TATN", "NVTK", "ALRS", "MTSS", "MGNT", "CHMF",
  "SNGS", "AFLT", "MOEX", "PHOR", "PLZL", "TCSG"
)

OUT_DIR <- "d:/Crypto-Analyzer-R/data"
dir.create(OUT_DIR, showWarnings = FALSE, recursive = TRUE)
OUT_FILE <- file.path(OUT_DIR, "tinkoff_ru_2yr.csv")

start_date <- Sys.Date() - 365 * 2
end_date   <- Sys.Date()

cat(sprintf("=== MOEX ISS Fetch: %d stocks, %s to %s ===\n",
            length(RU_STOCKS), format(start_date), format(end_date)))

all_data <- data.frame()

for (ticker in RU_STOCKS) {
  cat(sprintf("[%s] ", ticker))

  base_url <- paste0(
    "https://iss.moex.com/iss/history/engines/stock/markets/shares/securities/",
    ticker, ".json?from=", format(start_date), "&till=", format(end_date)
  )

  all_pages <- list()
  start <- 0
  fetched <- 0

  repeat {
    page_url <- if (start == 0) base_url else paste0(base_url, "&start=", start)

    resp <- tryCatch(fromJSON(page_url), error = function(e) { cat("ERR\n"); NULL })
    if (is.null(resp)) break

    hist <- resp$history
    if (is.null(hist)) { cat("no-history\n"); break }
    if (is.null(hist$data) || !is.matrix(hist$data) || nrow(hist$data) == 0) { cat("no-data\n"); break }

    data <- hist$data
    colnames(data) <- tolower(hist$columns)

    all_pages[[length(all_pages) + 1]] <- data
    n <- nrow(data)
    fetched <- fetched + n

    if (n < 100) break
    start <- start + n
    Sys.sleep(0.5)
  }

  if (fetched == 0) { cat("no data\n"); next }

  stock_df <- do.call(rbind, all_pages)

  dc <- which(colnames(stock_df) == "tradedate")[1]
  cc <- which(colnames(stock_df) == "legalcloseprice")[1]
  if (is.na(cc)) cc <- which(colnames(stock_df) == "close")[1]
  vc <- which(colnames(stock_df) == "volume")[1]

  if (is.na(dc) || is.na(cc)) {
    cat(sprintf("cols? tradedate:%d close:%d\n", dc, cc))
    next
  }

  df <- data.frame(
    ticker = ticker,
    date   = as.Date(as.character(stock_df[, dc])),
    close  = as.numeric(stock_df[, cc]),
    volume = if (!is.na(vc)) as.numeric(stock_df[, vc]) else NA_real_,
    market = "ru",
    stringsAsFactors = FALSE
  )

  df <- df[!is.na(df$date) & !is.na(df$close) & df$close > 0, ]
  df <- df[order(df$date), ]
  all_data <- rbind(all_data, df)
  cat(sprintf("%d rows\n", nrow(df)))
}

if (nrow(all_data) == 0) stop("No data fetched for any stock")

all_data <- all_data[!duplicated(all_data[, c("ticker", "date")]), ]
write.csv(all_data, OUT_FILE, row.names = FALSE)

cat(sprintf("\n=== DONE ===\nTotal: %s rows, %d tickers, %s to %s\nSaved: %s (%.1f MB)\n",
            format(nrow(all_data), big.mark=","), length(unique(all_data$ticker)),
            min(all_data$date), max(all_data$date),
            OUT_FILE, file.info(OUT_FILE)$size / 1024^2))
