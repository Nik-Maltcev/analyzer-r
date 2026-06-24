#!/usr/bin/env Rscript
# ── Fetch 2 years of daily candles for Russian stocks from Tinkoff API ──────
# Uses REST history-data endpoint: GET /history-data?figi=XXX&year=YYYY
# Returns ZIP archive of minute candles. We aggregate to daily OHLCV.
# Requires env var: TINKOFF_API_TOKEN (Bearer token)
# Output: data/tinkoff_ru_2yr.csv
# Run: Rscript scripts/fetch_tinkoff.R

library(httr)
library(jsonlite)

TOKEN <- Sys.getenv("TINKOFF_API_TOKEN", "")
if (nchar(TOKEN) < 10) stop("Set TINKOFF_API_TOKEN env var")

# ── Russian stocks: ticker + FIGI ───────────────────────────────────────────
RU_STOCKS <- list(
  list(ticker = "SBER", figi = "BBG004730N88", name = "Сбербанк"),
  list(ticker = "GAZP", figi = "BBG004730RP0", name = "Газпром"),
  list(ticker = "LKOH", figi = "BBG004731032", name = "Лукойл"),
  list(ticker = "GMKN", figi = "BBG004731489", name = "Норникель"),
  list(ticker = "ROSN", figi = "BBG004731354", name = "Роснефть"),
  list(ticker = "VTBR", figi = "BBG004730ZJ9", name = "ВТБ"),
  list(ticker = "TATN", figi = "BBG004RVFFC0", name = "Татнефть"),
  list(ticker = "NVTK", figi = "BBG00475KKY8", name = "НОВАТЭК"),
  list(ticker = "ALRS", figi = "BBG000R6V936", name = "АЛРОСА"),
  list(ticker = "MTSS", figi = "BBG004S681W1", name = "МТС"),
  list(ticker = "MGNT", figi = "BBG004S684M7", name = "Магнит"),
  list(ticker = "CHMF", figi = "BBG00475L812", name = "Северсталь"),
  list(ticker = "SNGS", figi = "BBG004S68829", name = "Сургутнефтегаз"),
  list(ticker = "AFLT", figi = "BBG004S681B4", name = "Аэрофлот"),
  list(ticker = "MOEX", figi = "BBG004730JJ5", name = "Мосбиржа"),
  list(ticker = "PHOR", figi = "BBG004S68672", name = "ФосАгро"),
  list(ticker = "PLZL", figi = "BBG000R607Y3", name = "Полюс"),
  list(ticker = "TCSG", figi = "BBG00QPYJ5H0", name = "ТКС Холдинг")
)

OUT_DIR <- "d:/Crypto-Analyzer-R/data"
dir.create(OUT_DIR, showWarnings = FALSE, recursive = TRUE)
OUT_FILE <- file.path(OUT_DIR, "tinkoff_ru_2yr.csv")

years <- c(as.integer(format(Sys.Date(), "%Y")) - 1, as.integer(format(Sys.Date(), "%Y")))
cat(sprintf("=== Tinkoff RU Stocks: %d tickers, years %d-%d ===\n",
            length(RU_STOCKS), years[1], years[2]))

all_data <- data.frame()

for (stock in RU_STOCKS) {
  cat(sprintf("[%s] %s (%s)...\n", stock$ticker, stock$name, stock$figi))
  stock_data <- data.frame()

  for (year in years) {
    url <- paste0("https://invest-public-api.tbank.ru/history-data?figi=",
                  stock$figi, "&year=", year)
    cat(sprintf("  Year %d: downloading...\n", year))

    # Download ZIP archive
    tmp_zip <- tempfile(fileext = ".zip")
    resp <- tryCatch(
      GET(url, add_headers(Authorization = paste("Bearer", TOKEN)),
          write_disk(tmp_zip, overwrite = TRUE), timeout(120)),
      error = function(e) NULL
    )

    if (is.null(resp) || status_code(resp) != 200) {
      cat(sprintf("  ERR: HTTP %d\n", if (is.null(resp)) 0 else status_code(resp)))
      next
    }

    # Extract and read CSV
    tmp_dir <- tempfile()
    dir.create(tmp_dir)
    unzip(tmp_zip, exdir = tmp_dir)
    csv_files <- list.files(tmp_dir, pattern = "\\.csv$", full.names = TRUE, recursive = TRUE)

    if (length(csv_files) == 0) {
      cat("  ERR: no CSV in archive\n")
      next
    }

    # Read minute data: columns are UID, UTC, open, close, high, low, volume
    df <- tryCatch(
      read.csv(csv_files[1], stringsAsFactors = FALSE, header = FALSE,
               col.names = c("uid", "ts", "open", "close", "high", "low", "volume")),
      error = function(e) NULL
    )

    if (is.null(df) || nrow(df) == 0) {
      cat("  ERR: empty CSV\n")
      next
    }

    # Aggregate minute -> daily
    df$date <- as.Date(substr(df$ts, 1, 10))
    df$volume <- as.numeric(df$volume)
    df$open <- as.numeric(df$open)
    df$high <- as.numeric(df$high)
    df$low <- as.numeric(df$low)
    df$close <- as.numeric(df$close)

    daily <- aggregate(cbind(open, close, high, low, volume) ~ date,
                       data = df,
                       FUN = function(x) {
                         if (length(x) == 1) return(x)
                         # For ohlc: open = first, close = last, high = max, low = min
                         c(x[1], tail(x, 1), max(x, na.rm = TRUE), min(x, na.rm = TRUE), sum(x, na.rm = TRUE))
                       })
    # Fix aggregate output format
    daily_df <- data.frame(
      ticker = stock$ticker,
      date   = daily$date,
      close  = daily$close[, 1],
      volume = daily$volume[, 1],
      market = "ru",
      stringsAsFactors = FALSE
    )

    stock_data <- rbind(stock_data, daily_df)
    cat(sprintf("  Year %d: %d days\n", year, nrow(daily_df)))

    unlink(tmp_zip); unlink(tmp_dir, recursive = TRUE)
  }

  if (nrow(stock_data) > 0) {
    all_data <- rbind(all_data, stock_data)
    cat(sprintf("[%s] Total: %d days\n", stock$ticker, nrow(stock_data)))
  }
}

# Save
all_data <- all_data[order(all_data$ticker, all_data$date), ]
write.csv(all_data, OUT_FILE, row.names = FALSE)

cat("\n=== DONE ===\n")
cat(sprintf("Total: %s rows, %d tickers, %s to %s\n",
            format(nrow(all_data), big.mark = ","),
            length(unique(all_data$ticker)),
            min(all_data$date), max(all_data$date)))
cat(sprintf("Saved: %s (%.1f MB)\n", OUT_FILE, file.info(OUT_FILE)$size / 1024^2))
