#!/usr/bin/env Rscript
# ── Daily update: fetch latest prices + compute signals ──────────────────────
# Runs via cron at 06:00 UTC (09:00 MSK) daily
# Requires env vars: TWELVEDATA_API_KEY, DB_PATH (optional)

library(RSQLite)
library(jsonlite)

source("scripts/tickers.R")

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
  # Fetch last 5 days to handle weekends/holidays
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
      # Only insert new rows
      existing <- dbGetQuery(con,
        sprintf("SELECT date FROM prices WHERE ticker = '%s' ORDER BY date DESC LIMIT 10", sym))
      new_rows <- df[!df$date %in% existing$date, ]
      if (nrow(new_rows) > 0) {
        dbWriteTable(con, "prices", new_rows, append = TRUE, row.names = FALSE)
        total_new <- total_new + nrow(new_rows)
      }
      ok_count <- ok_count + 1
    }

    if (b_idx < length(batches)) Sys.sleep(10)
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

# ── Compute signals ──────────────────────────────────────────────────────────
cat("Computing signals...\n")

# Engle-Granger cointegration (same as app.R)
engle_granger <- function(pa, pb) {
  ok <- !is.na(pa) & !is.na(pb) & pa > 0 & pb > 0
  if (sum(ok) < 60) return(list(halflife = NA, is_coint = FALSE, hedge_ratio = NA))
  la <- log(pa[ok]); lb <- log(pb[ok])
  tryCatch({
    fit   <- lm(la ~ lb)
    resid <- residuals(fit)
    hedge <- coef(fit)[2]
    n     <- length(resid)
    y     <- diff(resid)
    x     <- resid[-n]
    ar_fit <- lm(y ~ x)
    coefs  <- summary(ar_fit)$coefficients
    if (!"x" %in% rownames(coefs))
      return(list(halflife = NA, is_coint = FALSE, hedge_ratio = hedge))
    b      <- coefs["x", "Estimate"]
    se_b   <- coefs["x", "Std. Error"]
    t_stat <- if (!is.na(se_b) && se_b > 0) b / se_b else NA
    is_coint <- !is.na(t_stat) && t_stat < -2.9
    halflife <- if (!is.na(b) && b < 0) round(-log(2) / b) else NA
    list(halflife = halflife, is_coint = is_coint, hedge_ratio = hedge)
  }, error = function(e) list(halflife = NA, is_coint = FALSE, hedge_ratio = NA))
}

# Get price matrix for each market
compute_signals_for_market <- function(market_name, con) {
  prices <- dbGetQuery(con, sprintf(
    "SELECT ticker, date, close FROM prices WHERE market = '%s' ORDER BY ticker, date",
    market_name))

  if (nrow(prices) == 0) return(data.frame())

  # Pivot to wide format
  tickers <- unique(prices$ticker)
  if (length(tickers) < 2) return(data.frame())

  dates <- sort(unique(prices$date))
  pw <- matrix(NA, nrow = length(dates), ncol = length(tickers))
  colnames(pw) <- tickers
  rownames(pw) <- dates

  for (t in tickers) {
    sub <- prices[prices$ticker == t, ]
    idx <- match(sub$date, dates)
    pw[idx, t] <- sub$close
  }

  # Only pairs with >= 70% correlation
  # Use returns for correlation
  ret <- apply(pw, 2, function(x) c(NA, diff(log(x))))
  cor_mat <- cor(ret, use = "pairwise.complete.obs")

  pairs <- which(upper.tri(cor_mat) & abs(cor_mat) >= 0.7, arr.ind = TRUE)
  if (nrow(pairs) == 0) return(data.frame())

  signals <- list()
  for (i in seq_len(nrow(pairs))) {
    ta <- colnames(cor_mat)[pairs[i, 1]]
    tb <- colnames(cor_mat)[pairs[i, 2]]
    pa <- pw[, ta]; pb <- pw[, tb]
    corr <- cor_mat[pairs[i, 1], pairs[i, 2]]

    cg <- engle_granger(pa, pb)
    hr <- if (!is.na(cg$hedge_ratio)) cg$hedge_ratio else 1

    ok <- !is.na(pa) & !is.na(pb) & pa > 0 & pb > 0
    if (sum(ok) < 30) next

    spread <- log(pa[ok]) - hr * log(pb[ok])
    mn  <- mean(spread, na.rm = TRUE)
    sd1 <- sd(spread, na.rm = TRUE)
    if (is.na(sd1) || sd1 == 0) next

    zscore <- (spread - mn) / sd1
    z_now  <- tail(zscore, 1)

    # AR(1) forecast
    z_hat <- tryCatch({
      z_clean <- zscore[!is.na(zscore)]
      n_z <- length(z_clean)
      if (n_z < 20) stop("too few")
      zy <- z_clean[-1]; zx <- z_clean[-n_z]
      cf <- coef(lm(zy ~ zx))
      as.numeric(cf[1] + cf[2] * tail(z_clean, 1))
    }, error = function(e) z_now)

    # Signal
    if (z_now >= 2 || z_hat >= 2) {
      signal <- paste0("Short ", ta, " / Long ", tb)
      strength <- if (cg$is_coint) "Strong" else "Forecast"
    } else if (z_now <= -2 || z_hat <= -2) {
      signal <- paste0("Long ", ta, " / Short ", tb)
      strength <- if (cg$is_coint) "Strong" else "Forecast"
    } else {
      next  # no signal, skip
    }

    signals[[length(signals) + 1]] <- data.frame(
      date       = today,
      ticker_a   = ta,
      ticker_b   = tb,
      z_score    = round(z_now, 3),
      z_forecast = round(z_hat, 3),
      signal     = signal,
      strength   = strength,
      is_coint   = as.integer(cg$is_coint),
      corr       = round(corr, 3),
      stringsAsFactors = FALSE
    )
  }

  if (length(signals) == 0) return(data.frame())
  do.call(rbind, signals)
}

sig_crypto <- compute_signals_for_market("crypto", con)
sig_stocks <- compute_signals_for_market("stocks", con)
sig_forex  <- compute_signals_for_market("forex", con)

all_signals <- rbind(sig_crypto, sig_stocks, sig_forex)

if (nrow(all_signals) > 0) {
  # Remove today's old signals
  dbExecute(con, "DELETE FROM signals WHERE date = ?", params = list(today))
  dbWriteTable(con, "signals", all_signals, append = TRUE, row.names = FALSE)
  cat(sprintf("Signals saved: %d active signals for %s\n", nrow(all_signals), today))

  # Print top signals
  strong <- all_signals[all_signals$strength == "Strong", ]
  if (nrow(strong) > 0) {
    cat("\n=== STRONG SIGNALS ===\n")
    for (i in seq_len(min(10, nrow(strong)))) {
      s <- strong[i, ]
      cat(sprintf("  %s | Z=%.2f -> %.2f | corr=%.0f%%\n",
                  s$signal, s$z_score, s$z_forecast, s$corr * 100))
    }
  }
} else {
  cat("No active signals today.\n")
}

cat(sprintf("\n[%s] === Daily Update Complete ===\n", Sys.time()))
dbDisconnect(con)
