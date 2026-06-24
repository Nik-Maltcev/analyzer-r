#!/usr/bin/env Rscript
# ── Compute pairs analysis for all markets, store in DB ──────────────────────
# Called by:
#   - start.sh after initial DB build (auto-analysis on deploy)
#   - daily_update.R after fetching latest prices (daily background recalc)
# Stores results in `pairs` table (current snapshot, replaced each run).
# Also appends active signals to `signals` table (historical record).

library(RSQLite)

DB_PATH <- Sys.getenv("DB_PATH", "/data/market.db")

if (!file.exists(DB_PATH)) stop("Database not found: ", DB_PATH)

# ── Engle-Granger cointegration (same as app.R) ──────────────────────────────
engle_granger <- function(pa, pb) {
  ok <- !is.na(pa) & !is.na(pb) & pa > 0 & pb > 0
  if (sum(ok) < 60) return(list(halflife = NA, t_stat = NA, is_coint = FALSE, hedge_ratio = NA))
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
      return(list(halflife = NA, t_stat = NA, is_coint = FALSE, hedge_ratio = hedge))
    b      <- coefs["x", "Estimate"]
    se_b   <- coefs["x", "Std. Error"]
    t_stat <- if (!is.na(se_b) && se_b > 0) b / se_b else NA
    is_coint <- !is.na(t_stat) && t_stat < -2.9
    halflife <- if (!is.na(b) && b < 0) round(-log(2) / b) else NA
    list(halflife = halflife, t_stat = t_stat, is_coint = is_coint, hedge_ratio = hedge)
  }, error = function(e) list(halflife = NA, t_stat = NA, is_coint = FALSE, hedge_ratio = NA))
}

# ── Compute pairs for one market ─────────────────────────────────────────────
compute_market_pairs <- function(market_name, con) {
  prices <- dbGetQuery(con, sprintf(
    "SELECT ticker, date, close FROM prices WHERE market = '%s' ORDER BY ticker, date",
    market_name))

  if (nrow(prices) == 0) {
    cat(sprintf("[%s] No prices, skipping\n", market_name))
    return(invisible(NULL))
  }

  tickers <- unique(prices$ticker)
  if (length(tickers) < 2) {
    cat(sprintf("[%s] Only %d ticker, skipping\n", market_name, length(tickers)))
    return(invisible(NULL))
  }

  cat(sprintf("[%s] %d tickers -> %d pairs\n", market_name, length(tickers),
              length(tickers) * (length(tickers) - 1) / 2))

  # Pivot to wide matrix
  dates <- sort(unique(prices$date))
  pw <- matrix(NA, nrow = length(dates), ncol = length(tickers))
  colnames(pw) <- tickers
  rownames(pw) <- dates
  for (t in tickers) {
    sub <- prices[prices$ticker == t, ]
    pw[match(sub$date, dates), t] <- sub$close
  }

  # Returns for correlation
  ret <- apply(pw, 2, function(x) c(NA, diff(log(x))))
  cor_mat <- cor(ret, use = "pairwise.complete.obs")

  # All pairs (upper triangle)
  pairs_idx <- which(upper.tri(cor_mat), arr.ind = TRUE)
  n_pairs   <- nrow(pairs_idx)

  results <- vector("list", n_pairs)
  computed <- 0

  for (i in seq_len(n_pairs)) {
    ta <- colnames(cor_mat)[pairs_idx[i, 1]]
    tb <- colnames(cor_mat)[pairs_idx[i, 2]]
    corr <- cor_mat[pairs_idx[i, 1], pairs_idx[i, 2]]
    if (is.na(corr)) next

    pa <- pw[, ta]; pb <- pw[, tb]

    # Cointegration
    cg <- engle_granger(pa, pb)

    # Z-score + AR(1) forecast + signal
    ok <- !is.na(pa) & !is.na(pb) & pa > 0 & pb > 0
    z_now <- NA; z_hat <- NA
    signal <- "Ждать"; signal_type <- "wait"; strength <- "Нет"

    if (sum(ok) >= 30) {
      hr <- if (!is.na(cg$hedge_ratio)) cg$hedge_ratio else 1
      spread <- log(pa[ok]) - hr * log(pb[ok])
      mn  <- mean(spread, na.rm = TRUE)
      sd1 <- sd(spread, na.rm = TRUE)

      if (!is.na(sd1) && sd1 > 0) {
        zscore <- (spread - mn) / sd1
        z_now  <- tail(zscore, 1)

        # AR(1) forecast
        fc <- tryCatch({
          zc <- zscore[!is.na(zscore)]
          n_z <- length(zc)
          if (n_z < 20) stop("too few")
          cf <- coef(lm(zc[-1] ~ zc[-n_z]))
          as.numeric(cf[1] + cf[2] * tail(zc, 1))
        }, error = function(e) z_now)
        z_hat <- fc

        # Signal logic (same as app.R)
        if (z_now >= 2 || z_hat >= 2) {
          signal <- paste0("Шорт ", ta, " / Лонг ", tb)
          signal_type <- "short_a"
        } else if (z_now <= -2 || z_hat <= -2) {
          signal <- paste0("Лонг ", ta, " / Шорт ", tb)
          signal_type <- "long_a"
        }

        strength <- if (cg$is_coint && abs(z_now) >= 2) "Сильный"
                    else if (abs(z_hat) >= 2) "Прогнозный"
                    else if (abs(z_now) >= 1.5) "Формируется"
                    else "Нет"
      }
    }

    # Score (same as app.R)
    score <- abs(corr)
    score <- score + ifelse(cg$is_coint, 0.3, 0)
    score <- score + ifelse(!is.na(cg$halflife) & cg$halflife >= 5 & cg$halflife <= 60, 0.3, 0)

    computed <- computed + 1
    results[[computed]] <- data.frame(
      market      = market_name,
      ticker_a    = ta,
      ticker_b    = tb,
      corr        = corr,
      halflife    = cg$halflife,
      t_stat      = cg$t_stat,
      is_coint    = as.integer(cg$is_coint),
      hedge_ratio = cg$hedge_ratio,
      score       = score,
      z_now       = round(z_now, 3),
      z_forecast  = round(z_hat, 3),
      signal      = signal,
      signal_type = signal_type,
      strength    = strength,
      stringsAsFactors = FALSE
    )

    if (computed %% 500 == 0) cat(sprintf("  ...%d/%d\n", computed, n_pairs))
  }

  df <- do.call(rbind, Filter(Negate(is.null), results))
  if (is.null(df) || nrow(df) == 0) {
    cat(sprintf("[%s] No valid pairs\n", market_name))
    return(invisible(NULL))
  }

  # Replace existing pairs for this market
  dbExecute(con, "DELETE FROM pairs WHERE market = ?", params = list(market_name))
  dbWriteTable(con, "pairs", df, append = TRUE, row.names = FALSE)

  n_active <- sum(df$signal_type != "wait")
  cat(sprintf("[%s] %d pairs stored, %d active signals\n", market_name, nrow(df), n_active))
  invisible(df)
}

# ── Main ─────────────────────────────────────────────────────────────────────
con <- dbConnect(SQLite(), DB_PATH)
on.exit(dbDisconnect(con))

dbExecute(con, "CREATE TABLE IF NOT EXISTS pairs (
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
)")
dbExecute(con, "CREATE INDEX IF NOT EXISTS idx_pairs_market ON pairs(market)")
dbExecute(con, "CREATE INDEX IF NOT EXISTS idx_pairs_score   ON pairs(score DESC)")

cat("=== Computing pairs analysis ===\n")
compute_market_pairs("crypto", con)
compute_market_pairs("stocks", con)
compute_market_pairs("forex", con)

# Save today's active signals to signals table (historical record)
today  <- format(Sys.Date(), "%Y-%m-%d")
active <- dbGetQuery(con, "SELECT * FROM pairs WHERE signal_type != 'wait'")
if (nrow(active) > 0) {
  # Create signals table if it doesn't exist (for standalone runs)
  dbExecute(con, "
    CREATE TABLE IF NOT EXISTS signals (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      date TEXT, ticker_a TEXT, ticker_b TEXT,
      z_score REAL, z_forecast REAL, signal TEXT,
      strength TEXT, is_coint INTEGER, corr REAL,
      created_at TEXT DEFAULT (datetime('now'))
    )
  ")
  dbExecute(con, "DELETE FROM signals WHERE date = ?", params = list(today))
  sig_df <- data.frame(
    date       = today,
    ticker_a   = active$ticker_a,
    ticker_b   = active$ticker_b,
    z_score    = active$z_now,
    z_forecast = active$z_forecast,
    signal     = active$signal,
    strength   = active$strength,
    is_coint   = active$is_coint,
    corr       = active$corr,
    stringsAsFactors = FALSE
  )
  dbWriteTable(con, "signals", sig_df, append = TRUE, row.names = FALSE)
  cat(sprintf("\nSaved %d active signals to signals table for %s\n", nrow(sig_df), today))
}

cat("\n=== Analysis complete ===\n")
