library(shiny)
library(bslib)
library(dplyr)
library(tidyr)
library(ggplot2)
library(zoo)
library(DT)
library(RSQLite)

options(shiny.maxRequestSize = 50 * 1024^2)
options(shiny.server.maxRequestSize = 50 * 1024^2)

# Keep session alive: 24h idle timeout + auto-reconnect + JS heartbeat
options(shiny.idle.timeout = 86400000)       # 24 hours
options(shiny.autoreload = TRUE)
options(shiny.sanitize.errors = FALSE)

ORANGE <- "#f7931a"
BLUE   <- "#58a6ff"
GREEN  <- "#3fb950"
RED    <- "#f85149"
GRAY   <- "#8b949e"
BG     <- "#0a0e14"
CARD   <- "#0f1419"
CARD2  <- "#131922"
BORDER <- "#1c2333"
GLOW_BL <- "rgba(88,166,255,0.15)"
GLOW_GR <- "rgba(63,185,80,0.15)"
GLOW_RD <- "rgba(248,81,73,0.15)"
GLOW_OR <- "rgba(247,147,26,0.15)"

dark_theme <- theme_minimal(base_size = 13) +
  theme(
    plot.background  = element_rect(fill = CARD, color = NA),
    panel.background = element_rect(fill = CARD, color = NA),
    panel.grid.major = element_line(color = "#161d2a", linewidth = 0.4),
    panel.grid.minor = element_blank(),
    axis.text        = element_text(color = "#8b949e", size = 10),
    axis.title       = element_text(color = "#adbac7", size = 11, face = "plain"),
    plot.title       = element_text(color = "#e6edf3", face = "bold", size = 15, margin = margin(b = 4)),
    plot.subtitle    = element_text(color = GRAY, size = 10.5, margin = margin(b = 12)),
    legend.background = element_rect(fill = "transparent", color = NA),
    legend.text      = element_text(color = "#adbac7", size = 10),
    legend.title     = element_text(color = "#e6edf3", size = 10.5),
    legend.position  = "top",
    plot.margin      = margin(16, 16, 12, 16)
  )

# ── Helpers ──────────────────────────────────────────────────────────────────
corr_label <- function(r) {
  if (is.na(r)) return("—")
  if (r >= 0.8)  return("Очень сильная связь")
  if (r >= 0.6)  return("Сильная связь")
  if (r >= 0.4)  return("Умеренная связь")
  if (r <= -0.6) return("Сильная обратная")
  if (r <= -0.4) return("Умеренная обратная")
  return("Нет связи")
}
corr_pct <- function(r) {
  if (is.na(r)) return("—")
  paste0(round(abs(r) * 100), "%")
}
dot_color <- function(r) {
  if (is.na(r)) return(GRAY)
  if (r >= 0.8) return(GREEN)
  if (r >= 0.6) return("#7ee787")
  if (r >= 0.4) return(ORANGE)
  if (r <= -0.6) return(RED)
  if (r <= -0.4) return("#ff7b72")
  return(GRAY)
}
lag_color <- function(lag) {
  if (is.na(lag) || lag == 0) return(GRAY)
  if (abs(lag) <= 3) return(ORANGE)
  return(BLUE)
}

placeholder_msg <- function(msg = "Нажмите «Анализировать»") {
  div(style = "text-align:center;padding:80px 20px;color:#555;",
    div(style = "
      width:80px;height:80px;margin:0 auto 20px;border-radius:50%;
      background:linear-gradient(135deg, rgba(88,166,255,0.1), rgba(167,139,250,0.1));
      display:flex;align-items:center;justify-content:center;
      border:1px solid #21262d;",
      tags$i(class = "fas fa-chart-line fa-2x", style = "color:#58a6ff;")
    ),
    p(style = "font-size:1.1rem;color:#8b949e;font-weight:500;", msg),
    p(style = "font-size:0.82rem;color:#484f58;", "Данные обрабатываются локально"))
}

badge <- function(txt, col) {
  tags$span(style = paste0(
    "display:inline-block;padding:4px 12px;border-radius:20px;",
    "font-size:0.78rem;font-weight:600;color:#fff;",
    "background:", col, ";",
    "box-shadow:0 2px 8px ", col, "33;"),
    txt)
}

# ── Cointegration helpers (Engle-Granger, manual) ────────────────────────────
# Returns: list(halflife, pval_approx, is_cointegrated)
engle_granger <- function(pa, pb, max_lag = 2) {
  ok <- !is.na(pa) & !is.na(pb) & pa > 0 & pb > 0
  if (sum(ok) < 60) return(list(halflife = NA, score = NA, is_coint = FALSE,
                                t_stat = NA, hedge_ratio = NA))
  la <- log(pa[ok]); lb <- log(pb[ok])

  result <- tryCatch({
    # Step 1: OLS regression to get hedge ratio and residuals
    fit   <- lm(la ~ lb)
    resid <- residuals(fit)
    hedge <- coef(fit)[2]   # coefficient on lb

    # Step 2: AR(1) on Δresid ~ resid_{t-1}  (simplified ADF)
    n      <- length(resid)
    y      <- diff(resid)
    x      <- resid[-n]
    ar_fit <- lm(y ~ x)
    coefs  <- summary(ar_fit)$coefficients
    if (!"x" %in% rownames(coefs))
      return(list(halflife = NA, t_stat = NA, score = NA, is_coint = FALSE, hedge_ratio = hedge))
    b      <- coefs["x", "Estimate"]
    se_b   <- coefs["x", "Std. Error"]
    t_stat <- if (!is.na(se_b) && se_b > 0) b / se_b else NA

    is_coint <- !is.na(t_stat) && t_stat < -2.9
    halflife <- if (!is.na(b) && b < 0) round(-log(2) / b) else NA
    score    <- if (!is.na(t_stat)) round(abs(t_stat), 2) else NA

    list(halflife = halflife, t_stat = t_stat, score = score,
         is_coint = is_coint, hedge_ratio = hedge)
  }, error = function(e) {
    list(halflife = NA, t_stat = NA, score = NA, is_coint = FALSE, hedge_ratio = NA)
  })
  result
}

# ── Backtest stats for a single pair (used by "Понятные сигналы") ────────────
# Returns: list(n_trades, win_rate, avg_pnl, avg_hold, has_history)
pair_backtest_stats <- function(pw, ta, tb, hr) {
  # Normalize ticker names: pivot_wider converts '/' to '.' in column names
  ta <- if (ta %in% colnames(pw)) ta else gsub("/", ".", ta)
  tb <- if (tb %in% colnames(pw)) tb else gsub("/", ".", tb)
  if (!ta %in% colnames(pw) || !tb %in% colnames(pw)) return(NULL)
  pa <- as.numeric(pw[[ta]]); pb <- as.numeric(pw[[tb]])
  dates <- as.Date(rownames(pw))
  ok <- !is.na(pa) & !is.na(pb) & pa > 0 & pb > 0
  if (sum(ok) < 30) return(NULL)
  spread <- log(pa[ok]) - hr * log(pb[ok])
  mn  <- mean(spread, na.rm = TRUE)
  sd1 <- sd(spread, na.rm = TRUE)
  if (is.na(sd1) || sd1 == 0) return(NULL)
  z  <- (spread - mn) / sd1
  sp <- spread
  dt <- dates[ok]
  n  <- length(z)

  entry_z <- 2.0; exit_z <- 0.5; stop_z <- 3.5
  trades <- list()
  in_trade <- FALSE; entry_idx <- NA; entry_dir <- NA; entry_spread <- NA

  for (i in seq_len(n)) {
    zi <- z[i]
    if (is.na(zi)) next
    if (!in_trade) {
      if (zi >=  entry_z) { in_trade <- TRUE; entry_dir <- -1; entry_idx <- i; entry_spread <- sp[i] }
      if (zi <= -entry_z) { in_trade <- TRUE; entry_dir <-  1; entry_idx <- i; entry_spread <- sp[i] }
    } else {
      hit_stop <- (entry_dir == -1 && zi >= stop_z) || (entry_dir == 1 && zi <= -stop_z)
      hit_tp   <- abs(zi) <= exit_z
      if (hit_stop || hit_tp) {
        pnl_log <- entry_dir * (sp[i] - entry_spread)
        pnl_pct <- (exp(pnl_log) - 1) * 100
        hold    <- as.integer(dt[i] - dt[entry_idx])
        trades[[length(trades) + 1]] <- data.frame(
          pnl_pct = pnl_pct, hold_days = hold,
          result = if (hit_stop) "stop" else "tp",
          stringsAsFactors = FALSE)
        in_trade <- FALSE
      }
    }
  }

  if (length(trades) == 0)
    return(list(n_trades = 0, win_rate = NA, avg_pnl = NA, avg_hold = NA,
                avg_win = NA, avg_loss = NA, sd_spread_pct = round(sd1 * 100, 3),
                has_history = FALSE))
  tdf <- do.call(rbind, trades)
  wins   <- tdf[tdf$pnl_pct > 0, ]
  losses <- tdf[tdf$pnl_pct <= 0, ]
  list(
    n_trades      = nrow(tdf),
    win_rate      = round(nrow(wins) / nrow(tdf) * 100),
    avg_pnl       = round(mean(tdf$pnl_pct), 2),
    avg_hold      = round(mean(tdf$hold_days)),
    avg_win       = if (nrow(wins)   > 0) round(mean(wins$pnl_pct), 2)   else NA,
    avg_loss      = if (nrow(losses) > 0) round(mean(losses$pnl_pct), 2) else NA,
    sd_spread_pct = round(sd1 * 100, 3),
    has_history   = TRUE
  )
}

# ── Full trade history for a single pair (used by "Максимальный профит") ────
# Returns data.frame with per-trade details: entry/exit dates, direction, pnl
pair_trades_history <- function(pw, ta, tb, hr) {
  # Normalize ticker names: pivot_wider converts '/' to '.' in column names
  ta <- if (ta %in% colnames(pw)) ta else gsub("/", ".", ta)
  tb <- if (tb %in% colnames(pw)) tb else gsub("/", ".", tb)
  if (!ta %in% colnames(pw) || !tb %in% colnames(pw)) return(NULL)
  pa <- as.numeric(pw[[ta]]); pb <- as.numeric(pw[[tb]])
  dates <- as.Date(rownames(pw))
  ok <- !is.na(pa) & !is.na(pb) & pa > 0 & pb > 0
  if (sum(ok) < 30) return(NULL)
  spread <- log(pa[ok]) - hr * log(pb[ok])
  mn  <- mean(spread, na.rm = TRUE)
  sd1 <- sd(spread, na.rm = TRUE)
  if (is.na(sd1) || sd1 == 0) return(NULL)
  z  <- (spread - mn) / sd1
  sp <- spread
  dt <- dates[ok]
  n  <- length(z)

  entry_z <- 2.0; exit_z <- 0.5; stop_z <- 3.5
  trades <- list()
  in_trade <- FALSE; entry_idx <- NA; entry_dir <- NA; entry_spread <- NA

  for (i in seq_len(n)) {
    zi <- z[i]
    if (is.na(zi)) next
    if (!in_trade) {
      if (zi >=  entry_z) { in_trade <- TRUE; entry_dir <- -1; entry_idx <- i; entry_spread <- sp[i] }
      if (zi <= -entry_z) { in_trade <- TRUE; entry_dir <-  1; entry_idx <- i; entry_spread <- sp[i] }
    } else {
      hit_stop <- (entry_dir == -1 && zi >= stop_z) || (entry_dir == 1 && zi <= -stop_z)
      hit_tp   <- abs(zi) <= exit_z
      if (hit_stop || hit_tp) {
        pnl_log <- entry_dir * (sp[i] - entry_spread)
        pnl_pct <- (exp(pnl_log) - 1) * 100
        hold    <- as.integer(dt[i] - dt[entry_idx])
        trades[[length(trades) + 1]] <- data.frame(
          pair        = paste0(ta, " / ", tb),
          ticker_a    = ta, ticker_b = tb,
          entry_date  = format(dt[entry_idx], "%Y-%m-%d"),
          exit_date   = format(dt[i],          "%Y-%m-%d"),
          direction   = if (entry_dir == -1) paste0("Шорт ", ta, " / Лонг ", tb)
                        else                  paste0("Лонг ", ta, " / Шорт ", tb),
          entry_z     = round(z[entry_idx], 2),
          exit_z      = round(zi, 2),
          hold_days   = hold,
          pnl_pct     = round(pnl_pct, 2),
          result      = if (hit_stop) "Стоп-лосс" else "Тейк-профит",
          hedge_ratio = hr,
          stringsAsFactors = FALSE)
        in_trade <- FALSE
      }
    }
  }
  if (length(trades) == 0) return(NULL)
  do.call(rbind, trades)
}

# ── Calculator: compute P&L for a signal (MEXC Perpetual) ────────────────────
# s must have: signal_type, z_now, halflife, bt, strength
# Returns list with all values needed for calc_block_ui()
calc_signal_pnl <- function(s, cap, lev, taker_fee, fund_rate) {
  pos_size <- cap * lev
  leg_size <- pos_size / 2

  # Hold time estimate (defensive against NA/NULL)
  hold_days <- if (!is.na(s$halflife) && s$halflife > 0) s$halflife
               else if (!is.null(s$bt) && isTRUE(s$bt$has_history) && !is.na(s$bt$avg_hold)) s$bt$avg_hold
               else 10
  hold_txt <- if (!is.na(s$halflife) && s$halflife > 0)
    paste0("~", s$halflife, " дн. (полупериод)")
    else if (!is.null(s$bt) && isTRUE(s$bt$has_history) && !is.na(s$bt$avg_hold))
      paste0("~", s$bt$avg_hold, " дн. (по истории)")
      else "~10 дн. (по умолчанию)"

  # Profit estimates
  z_abs <- suppressWarnings(abs(s$z_now))
  if (is.na(z_abs)) z_abs <- 2

  has_hist <- !is.null(s$bt) && isTRUE(s$bt$has_history) && !is.na(s$bt$avg_win)
  if (has_hist) {
    tp_pct <- s$bt$avg_win
    sl_pct <- if (!is.na(s$bt$avg_loss)) abs(s$bt$avg_loss) else 0
    src_txt <- paste0("по истории (", s$bt$n_trades, " сделок)")
  } else {
    sd_pct <- if (!is.null(s$bt) && !is.null(s$bt$sd_spread_pct) &&
                  !is.na(s$bt$sd_spread_pct) && s$bt$sd_spread_pct > 0) s$bt$sd_spread_pct else 1
    tp_pct <- round((z_abs - 0.5) * sd_pct, 2)
    sl_pct <- round((3.5 - z_abs) * sd_pct, 2)
    src_txt <- "теоретический расчёт"
  }
  if (is.na(tp_pct) || tp_pct <= 0) tp_pct <- 0.1
  if (is.na(sl_pct) || sl_pct <= 0) sl_pct <- 0.1

  # Costs (USDT)
  comm <- round(4 * leg_size * taker_fee / 100, 2)
  fund_periods <- hold_days * 3
  funding <- round(pos_size * fund_rate / 100 * fund_periods, 2)

  # Net P&L
  gross_tp <- round(pos_size * tp_pct / 100, 2)
  gross_sl <- round(pos_size * sl_pct / 100, 2)
  net_tp   <- round(gross_tp - comm - funding, 2)
  net_sl   <- round(-(gross_sl + comm + funding), 2)
  rr_ratio <- if (!is.na(net_sl) && net_sl != 0) round(abs(net_tp / net_sl), 2) else NA

  list(
    pos_size = pos_size, leg_size = leg_size, comm = comm, funding = funding,
    fund_periods = fund_periods, gross_tp = gross_tp, gross_sl = gross_sl,
    net_tp = net_tp, net_sl = net_sl, rr_ratio = rr_ratio,
    tp_pct = tp_pct, sl_pct = sl_pct, hold_days = hold_days,
    hold_txt = hold_txt, src_txt = src_txt,
    tp_col = if (!is.na(net_tp) && net_tp > 0) GREEN else RED
  )
}

# ── Calculator: render the P&L block UI (shared by both tabs) ────────────────
calc_block_ui <- function(v) {
  if (is.null(v)) return(div())
  fmt <- function(x) if (is.null(x) || is.na(x)) "—" else format(x, big.mark = " ", scientific = FALSE, trim = TRUE)
  rr_col <- if (!is.null(v$rr_ratio) && !is.na(v$rr_ratio) && v$rr_ratio >= 1.5) GREEN else ORANGE
  tp_txt <- if (!is.null(v$tp_pct) && !is.na(v$tp_pct)) paste0(if (v$tp_pct > 0) "+" else "", round(v$tp_pct, 2), "%") else "—"
  sl_txt <- if (!is.null(v$sl_pct) && !is.na(v$sl_pct)) paste0("-", abs(round(v$sl_pct, 2)), "%") else "—"
  tp_col <- if (!is.null(v$tp_col)) v$tp_col else GREEN
  net_tp_v <- if (!is.null(v$net_tp)) v$net_tp else 0
  net_sl_v <- if (!is.null(v$net_sl)) v$net_sl else 0
  gross_tp_v <- if (!is.null(v$gross_tp)) v$gross_tp else 0
  gross_sl_v <- if (!is.null(v$gross_sl)) v$gross_sl else 0
  comm_v <- if (!is.null(v$comm)) v$comm else 0
  fund_v <- if (!is.null(v$funding)) v$funding else 0
  pos_size_v <- if (!is.null(v$pos_size)) v$pos_size else 0
  hold_d_v <- if (!is.null(v$hold_days)) v$hold_days else 0
  fund_per_v <- if (!is.null(v$fund_periods)) v$fund_periods else 0
  rr_v <- if (!is.null(v$rr_ratio) && !is.na(v$rr_ratio)) round(v$rr_ratio, 1) else NULL
  
  div(style = paste0("margin-top:14px;padding:14px 16px;border-radius:10px;",
                     "background:", CARD, ";border:1px solid ", BORDER, ";"),
    div(style = "font-size:0.85rem;font-weight:600;color:#e6edf3;margin-bottom:10px;",
      "Калькулятор прибыли (MEXC Perpetual)"),
    layout_columns(col_widths = c(3, 3, 3, 3),
      div(
        div(style = "font-size:0.72rem;color:#8b949e;", "Размер позиции"),
        div(style = "font-size:0.95rem;font-weight:600;color:#e6edf3;", paste0("$", fmt(pos_size_v))),
        div(style = "font-size:0.68rem;color:#555;", "капитал × плечо")
      ),
      div(
        div(style = "font-size:0.72rem;color:#8b949e;", "Комиссии (вход+выход)"),
        div(style = "font-size:0.95rem;font-weight:600;color:#f85149;", paste0("-$", fmt(comm_v))),
        div(style = "font-size:0.68rem;color:#555;", "4 заполнения × taker%")
      ),
      div(
        div(style = "font-size:0.72rem;color:#8b949e;", paste0("Финансирование (", fund_per_v, " раз)")),
        div(style = "font-size:0.95rem;font-weight:600;color:#f85149;", paste0("-$", fmt(fund_v))),
        div(style = "font-size:0.68rem;color:#555;", paste0("за ", hold_d_v, " дн."))
      ),
      div(
        div(style = "font-size:0.72rem;color:#8b949e;", "Risk / Reward"),
        div(style = paste0("font-size:1.1rem;font-weight:700;color:", rr_col, ";"),
          if (!is.null(rr_v)) paste0("1:", rr_v) else "—"),
        div(style = "font-size:0.68rem;color:#555;", "профит / убыток")
      )
    ),
    div(style = "border-top:1px solid #30363d;margin:10px 0;"),
    layout_columns(col_widths = c(6, 6),
      div(style = paste0("text-align:center;padding:10px;border-radius:8px;",
                         "background:#0f2a1a;border:1px solid ", GREEN, ";"),
        div(style = "font-size:0.75rem;color:#8b949e;", "Чистая прибыль (TP)"),
        div(style = paste0("font-size:1.3rem;font-weight:700;color:", tp_col, ";"),
          paste0(if (net_tp_v > 0) "+" else "", "$", net_tp_v)),
        div(style = paste0("font-size:0.72rem;color:", GREEN, ";font-weight:600;"), tp_txt),
        div(style = "font-size:0.68rem;color:#555;",
          paste0(if (gross_tp_v > 0) "+" else "", gross_tp_v, " − ", comm_v, " − ", fund_v))
      ),
      div(style = paste0("text-align:center;padding:10px;border-radius:8px;",
                         "background:#2a0f0f;border:1px solid ", RED, ";"),
        div(style = "font-size:0.75rem;color:#8b949e;", "Чистый убыток (SL)"),
        div(style = "font-size:1.3rem;font-weight:700;color:#f85149;", paste0("$", net_sl_v)),
        div(style = paste0("font-size:0.72rem;color:", RED, ";font-weight:600;"), sl_txt),
        div(style = "font-size:0.68rem;color:#555;",
          paste0("-(", gross_sl_v, " + ", comm_v, " + ", fund_v, ")"))
      )
    )
  )
}

# ── Calculator: settings inputs block (shared layout) ───────────────────────
calc_settings_ui <- function(prefix) {
  layout_columns(col_widths = c(3, 3, 3, 3),
    div(
      tags$label(style = "font-size:0.78rem;color:#8b949e;", "Капитал на сделку (USDT)"),
      numericInput(paste0(prefix, "capital"), NULL, value = 100, min = 10, max = 100000, step = 10, width = "100%")
    ),
    div(
      tags$label(style = "font-size:0.78rem;color:#8b949e;", "Плечо"),
      sliderInput(paste0(prefix, "leverage"), NULL, min = 1, max = 20, value = 1, step = 1, width = "100%", post = "x")
    ),
    div(
      tags$label(style = "font-size:0.78rem;color:#8b949e;", "Комиссия taker (% / сторону)"),
      numericInput(paste0(prefix, "taker"), NULL, value = 0.02, min = 0, max = 1, step = 0.01, width = "100%")
    ),
    div(
      tags$label(style = "font-size:0.78rem;color:#8b949e;", "Финансирование (% / 8ч)"),
      numericInput(paste0(prefix, "funding"), NULL, value = 0.01, min = 0, max = 0.1, step = 0.005, width = "100%")
    )
  )
}

# ── Read calculator inputs by prefix (with fallbacks) ───────────────────────
get_calc_inputs <- function(input, prefix) {
  cap <- if (isTruthy(input[[paste0(prefix, "capital")]]))  input[[paste0(prefix, "capital")]]  else 100
  lev <- if (isTruthy(input[[paste0(prefix, "leverage")]])) input[[paste0(prefix, "leverage")]] else 1
  tk  <- if (isTruthy(input[[paste0(prefix, "taker")]]))    input[[paste0(prefix, "taker")]]    else 0.02
  fd  <- if (isTruthy(input[[paste0(prefix, "funding")]]))  input[[paste0(prefix, "funding")]]  else 0.01
  list(cap = cap, lev = lev, taker = tk, funding = fd)
}

halflife_label <- function(hl) {
  if (is.na(hl) || hl <= 0) return("Нет возврата")
  if (hl <= 5)   return(paste0(hl, " дн. — слишком быстро"))
  if (hl <= 30)  return(paste0(hl, " дн. — отлично для трейдинга"))
  if (hl <= 90)  return(paste0(hl, " дн. — приемлемо"))
  return(paste0(hl, " дн. — слишком медленно"))
}
halflife_color <- function(hl) {
  if (is.na(hl) || hl <= 0) return(GRAY)
  if (hl <= 5)   return(BLUE)
  if (hl <= 30)  return(GREEN)
  if (hl <= 90)  return(ORANGE)
  return(RED)
}

# ── UI ───────────────────────────────────────────────────────────────────────
ui <- page_navbar(
  title = div(
    style = "display:flex;align-items:center;gap:12px;padding:4px 0;",
    tags$span(style = "
      display:inline-flex;align-items:center;justify-content:center;
      width:34px;height:34px;border-radius:10px;
      background:linear-gradient(135deg, #58a6ff 0%, #a78bfa 50%, #f7931a 100%);
      box-shadow:0 4px 16px rgba(88,166,255,0.35);
      font-size:1.1rem;font-weight:900;color:#0a0e14;",
      "C"),
    div(style = "display:flex;flex-direction:column;line-height:1.1;",
      tags$span(style = "
        font-size:1.15rem;font-weight:800;letter-spacing:-0.02em;
        background:linear-gradient(135deg, #e6edf3 0%, #a78bfa 100%);
        -webkit-background-clip:text;-webkit-text-fill-color:transparent;
        background-clip:text;", "CryptoScope"),
      tags$span(style = "font-size:0.62rem;color:#555;font-weight:500;letter-spacing:0.05em;text-transform:uppercase;",
        "pairs trading terminal")
    )
  ),
  theme = bs_theme(
    bg = BG, fg = "#e6edf3", primary = BLUE, secondary = BORDER,
    base_font = font_google("Inter"),
    "navbar-bg"          = CARD,
    "card-bg"            = CARD,
    "card-border-color"  = BORDER,
    "input-bg"           = CARD2,
    "input-border-color" = BORDER,
    "input-color"        = "#e6edf3"
  ),
  fillable = FALSE,
  header = tags$head(tags$style(HTML("
    /* ════════════════════════════════════════════════════════════════════
       CryptoScope — Modern Fintech Design System
       Glassmorphism + neon accents + smooth animations
       ════════════════════════════════════════════════════════════════════ */

    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@500;600;700&display=swap');

    :root {
      --bg: #0a0e14;
      --card: #0f1419;
      --card2: #131922;
      --border: #1c2333;
      --border-hover: #2a3548;
      --text: #e6edf3;
      --text-dim: #8b949e;
      --text-muted: #555c6b;
      --blue: #58a6ff;
      --green: #3fb950;
      --red: #f85149;
      --orange: #f7931a;
      --purple: #a78bfa;
      --glow-blue: rgba(88,166,255,0.2);
      --glow-green: rgba(63,185,80,0.2);
      --glow-red: rgba(248,81,73,0.2);
    }

    * { box-sizing: border-box; }

    body {
      letter-spacing: -0.01em;
      background: var(--bg);
      background-image:
        radial-gradient(ellipse 80% 50% at 50% -20%, rgba(88,166,255,0.06), transparent),
        radial-gradient(ellipse 60% 40% at 80% 100%, rgba(167,139,250,0.04), transparent);
      background-attachment: fixed;
      font-feature-settings: 'cv11', 'ss01';
    }

    /* ── Navbar ──────────────────────────────────────────────────────────── */
    .navbar {
      border-bottom: 1px solid var(--border) !important;
      backdrop-filter: blur(20px) saturate(180%);
      -webkit-backdrop-filter: blur(20px) saturate(180%);
      background: rgba(15,20,25,0.7) !important;
      padding: 6px 20px;
    }
    .nav-link {
      font-weight: 500;
      font-size: 0.88rem;
      color: var(--text-dim) !important;
      transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
      border-radius: 8px;
      padding: 8px 14px !important;
      position: relative;
    }
    .nav-link:hover {
      color: var(--blue) !important;
      background: var(--glow-blue);
      transform: translateY(-1px);
    }
    .nav-link.active {
      color: var(--blue) !important;
      background: rgba(88,166,255,0.08);
    }
    .nav-link.active::after {
      content: '';
      position: absolute;
      bottom: -7px; left: 50%;
      transform: translateX(-50%);
      width: 24px; height: 2px;
      background: var(--blue);
      border-radius: 2px;
      box-shadow: 0 0 8px var(--blue);
    }

    /* ── Cards (glassmorphism) ───────────────────────────────────────────── */
    .card {
      border-radius: 16px !important;
      border: 1px solid var(--border) !important;
      background: linear-gradient(180deg, var(--card) 0%, var(--card2) 100%) !important;
      box-shadow: 0 1px 0 rgba(255,255,255,0.02) inset, 0 8px 32px rgba(0,0,0,0.3);
      transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    }
    .card:hover {
      border-color: var(--border-hover) !important;
      box-shadow: 0 1px 0 rgba(255,255,255,0.03) inset, 0 12px 40px rgba(0,0,0,0.4);
      transform: translateY(-1px);
    }
    .card-header {
      font-weight: 600;
      font-size: 0.92rem;
      color: var(--text);
      border-bottom: 1px solid var(--border) !important;
      padding: 16px 20px !important;
      background: transparent !important;
    }
    .card-body { padding: 20px !important; }

    /* ── Buttons ─────────────────────────────────────────────────────────── */
    .btn-primary {
      background: linear-gradient(135deg, #58a6ff 0%, #388bfd 100%) !important;
      border: none !important;
      border-radius: 10px !important;
      font-weight: 600;
      font-size: 0.9rem;
      padding: 10px 22px;
      transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
      box-shadow: 0 4px 16px rgba(88,166,255,0.3);
    }
    .btn-primary:hover {
      transform: translateY(-2px);
      box-shadow: 0 8px 24px rgba(88,166,255,0.45);
      filter: brightness(1.1);
    }
    .btn-primary:active { transform: translateY(0); }
    .btn-secondary {
      background: var(--card2) !important;
      border: 1px solid var(--border) !important;
      color: var(--text) !important;
      border-radius: 10px !important;
      font-weight: 500;
      transition: all 0.2s;
    }
    .btn-secondary:hover {
      border-color: var(--border-hover) !important;
      background: var(--card) !important;
    }
    .btn-sm { font-size: 0.78rem; padding: 6px 12px; }

    /* ── Inputs ──────────────────────────────────────────────────────────── */
    .form-control, .form-select {
      border-radius: 10px !important;
      background-color: var(--card2) !important;
      border: 1px solid var(--border) !important;
      color: var(--text) !important;
      transition: all 0.2s;
      font-size: 0.88rem;
    }
    .form-control:focus, .form-select:focus {
      border-color: var(--blue) !important;
      box-shadow: 0 0 0 3px var(--glow-blue) !important;
      background-color: var(--card) !important;
    }
    select.form-select, select.form-control {
      border-radius: 10px !important;
      background-color: var(--card2) !important;
      border: 1px solid var(--border) !important;
    }

    /* ── Radio buttons (segmented control look) ──────────────────────────── */
    .radio-inline .radio { display: inline-flex; }
    .radio-inline label {
      padding: 8px 16px;
      border: 1px solid var(--border);
      border-radius: 10px;
      margin-right: 8px;
      cursor: pointer;
      transition: all 0.2s;
      font-size: 0.85rem;
      font-weight: 500;
      color: var(--text-dim);
    }
    .radio-inline label:hover {
      border-color: var(--blue);
      color: var(--text);
    }
    .radio-inline input:checked + span {
      color: var(--blue);
      font-weight: 600;
    }

    /* ── Checkbox ────────────────────────────────────────────────────────── */
    .form-check-input:checked {
      background-color: var(--blue) !important;
      border-color: var(--blue) !important;
    }
    .form-check-input:focus {
      border-color: var(--blue) !important;
      box-shadow: 0 0 0 3px var(--glow-blue) !important;
    }

    /* ── Value boxes ─────────────────────────────────────────────────────── */
    .bslib-value-box {
      border-radius: 14px !important;
      border: 1px solid var(--border) !important;
      background: linear-gradient(135deg, var(--card) 0%, var(--card2) 100%) !important;
      transition: all 0.3s;
    }
    .bslib-value-box:hover {
      border-color: var(--border-hover) !important;
      transform: translateY(-2px);
    }

    /* ── Tables (DT) ─────────────────────────────────────────────────────── */
    .dataTables_wrapper { font-size: 0.84rem; }
    table.dataTable {
      border-collapse: separate !important;
      border-spacing: 0 !important;
    }
    table.dataTable thead th {
      border-bottom: 2px solid var(--border) !important;
      font-weight: 600;
      color: var(--text);
      text-transform: uppercase;
      font-size: 0.72rem;
      letter-spacing: 0.05em;
      padding: 12px 14px !important;
    }
    table.dataTable tbody td {
      padding: 10px 14px !important;
      border-bottom: 1px solid rgba(28,35,51,0.5) !important;
    }
    table.dataTable tbody tr {
      transition: all 0.15s ease;
    }
    table.dataTable tbody tr:hover {
      background: var(--card2) !important;
    }
    .table-dark { background: transparent !important; color: var(--text) !important; }

    /* ── Progress / notifications ────────────────────────────────────────── */
    .shiny-notification {
      background: rgba(15,20,25,0.95) !important;
      backdrop-filter: blur(20px);
      border: 1px solid var(--border) !important;
      border-radius: 12px !important;
      color: var(--text) !important;
      box-shadow: 0 8px 32px rgba(0,0,0,0.5);
    }
    .progress-bar {
      background: linear-gradient(90deg, var(--blue), var(--purple)) !important;
    }

    /* ── Scrollbar ───────────────────────────────────────────────────────── */
    ::-webkit-scrollbar { width: 10px; height: 10px; }
    ::-webkit-scrollbar-track { background: var(--bg); }
    ::-webkit-scrollbar-thumb {
      background: var(--border);
      border-radius: 5px;
      border: 2px solid var(--bg);
    }
    ::-webkit-scrollbar-thumb:hover { background: var(--border-hover); }

    /* ── Numbers (monospace for financial data) ──────────────────────────── */
    .num, .dataTables_wrapper td {
      font-variant-numeric: tabular-nums;
    }

    /* ── Animations ──────────────────────────────────────────────────────── */
    @keyframes fadeInUp {
      from { opacity: 0; transform: translateY(8px); }
      to   { opacity: 1; transform: translateY(0); }
    }
    @keyframes pulse-glow {
      0%, 100% { box-shadow: 0 0 8px currentColor; }
      50%      { box-shadow: 0 0 16px currentColor; }
    }
    @keyframes spin {
      from { transform: rotate(0deg); }
      to   { transform: rotate(360deg); }
    }
    .card, .bslib-value-box { animation: fadeInUp 0.4s ease-out; }

    /* ── HR ──────────────────────────────────────────────────────────────── */
    hr { border-color: var(--border) !important; opacity: 0.5; }

    /* ── Slider (range) ──────────────────────────────────────────────────── */
    .irs--shiny .irs-bar { background: var(--blue) !important; }
    .irs--shiny .irs-handle {
      background: var(--blue) !important;
      box-shadow: 0 0 0 4px var(--glow-blue);
    }
  "))),

  # ── Keep-alive: heartbeat + auto-reconnect ────────────────────────────────
  tags$script(HTML("
    // Heartbeat: ping Shiny every 60s to prevent idle timeout
    $(function() {
      setInterval(function() {
        if (typeof Shiny !== 'undefined' && Shiny.shinyapp) {
          Shiny.shinyapp.makeRequest('heartbeat', [], []);
        }
      }, 60000);
    });

    // Auto-reconnect on WebSocket drop
    $(document).on('shiny:disconnected', function(e) {
      setTimeout(function() { window.location.reload(); }, 3000);
    });

    // Prevent browser sleep (Wake Lock API)
    if ('wakeLock' in navigator) {
      var wakeLock = null;
      navigator.wakeLock.request('screen').catch(function(){});
      document.addEventListener('visibilitychange', function() {
        if (wakeLock !== null && document.visibilityState === 'visible') {
          navigator.wakeLock.request('screen').catch(function(){});
        }
      });
    }
  ")),

  # ── Auth status bar ───────────────────────────────────────────────────────
  div(id = "auth_bar",
    style = "display:flex;align-items:center;justify-content:flex-end;padding:8px 20px;gap:10px;background:#0f1419;border-bottom:1px solid #1c2333;",
    uiOutput("auth_status_ui")
  ),

  # ── TAB 1: Данные ───────────────────────────────────────────────────────
  nav_panel("📂 Данные",
    layout_columns(col_widths = c(4, 8),
      card(
        card_header("Источник данных"),
        card_body(
          # Market type switcher
          radioButtons("market_type", NULL,
            choices = c("Crypto" = "crypto", "Акции/ETF" = "stocks", "RU" = "ru"),
            selected = "crypto", inline = TRUE),

          hr(),

          # DB status
          uiOutput("db_status_ui"),

          hr(),

          # Auto-analysis status
          uiOutput("analysis_status_ui")
        )
      ),
      card(
        card_header("Предпросмотр данных"),
        card_body(uiOutput("data_summary"), DTOutput("preview_table"))
      )
    )
  ),

  # ── TAB 2: Pairs Trading ─────────────────────────────────────────────────
  nav_panel("🤝 Pairs Trading",
    uiOutput("pairs_ui")
  ),

  # ── TAB 3: Сигналы ──────────────────────────────────────────────────────
  nav_panel("🚦 Сигналы",
    div(style = "padding:16px 20px;border-radius:12px;border:1px solid #30363d;background:#161b22;margin-bottom:18px;",
      div(style = "font-size:0.9rem;font-weight:600;color:#e6edf3;margin-bottom:12px;",
        "Настройки калькулятора (применяются ко всем сигналам)"),
      calc_settings_ui("sigcalc_"),
      div(style = "font-size:0.72rem;color:#555;margin-top:8px;",
        "MEXC Perpetual: taker 0.02%, maker 0.00%, финансирование ~0.01% / 8ч. ",
        "Комиссии: 4 заполнения (2 ноги × вход + выход). Измените под свой аккаунт.")
    ),
    div(style = "padding:12px 20px;border-radius:12px;border:1px solid #30363d;background:#161b22;margin-bottom:18px;",
      div(style = "font-size:0.9rem;font-weight:600;color:#e6edf3;margin-bottom:6px;", "Режим:"),
      radioButtons("signals_mode", NULL,
        choices = c("🚦 Сигналы" = "all", "💎 Прогноз" = "forecast", "⚡ Быстрые (<7д)" = "short"),
        selected = "all", inline = TRUE)
    ),
    uiOutput("signals_ui")
  ),

  # ── TAB 5: Максимальный профит ──────────────────────────────────────────
  nav_panel("💎 Макс. профит",
    div(style = "padding:16px 20px;border-radius:12px;border:1px solid #1c2333;background:#0f1419;margin-bottom:18px;",
      div(style = "font-size:0.9rem;font-weight:600;color:#e6edf3;margin-bottom:12px;",
        "Настройки калькулятора (применяются ко всем сделкам)"),
      calc_settings_ui("mpcalc_"),
      div(style = "font-size:0.72rem;color:#555c6b;margin-top:8px;",
        "MEXC Perpetual: taker 0.02%, maker 0.00%, финансирование ~0.01% / 8ч. ",
        "Комиссии: 4 заполнения (2 ноги × вход + выход). Измените под свой аккаунт.")
    ),
    div(style = "padding:14px 20px;border-radius:12px;border:1px solid #1c2333;background:#0f1419;margin-bottom:18px;",
      div(style = "font-size:0.9rem;font-weight:600;color:#e6edf3;margin-bottom:10px;",
        "Фильтры пар"),
      layout_columns(col_widths = c(4, 4, 4),
        checkboxInput("mp_coint_only", "Только коинтегрированные", value = FALSE),
        sliderInput("mp_min_corr", "Мин. корреляция", min = 50, max = 100, value = 50, step = 5, post = "%", width = "100%"),
        sliderInput("mp_min_trades", "Мин. сделок в истории", min = 1, max = 20, value = 3, step = 1, width = "100%")
      )
    ),
    uiOutput("maxprofit_ui")
  ),

  # ── TAB 6: Короткие сделки (до 7 дней) ──────────────────────────────────
  nav_panel("⚡ Короткие сделки",
    div(style = "padding:16px 20px;border-radius:12px;border:1px solid #1c2333;background:#0f1419;margin-bottom:18px;",
      div(style = "font-size:0.9rem;font-weight:600;color:#e6edf3;margin-bottom:12px;",
        "Настройки калькулятора (применяются ко всем сделкам)"),
      calc_settings_ui("shortcalc_"),
      div(style = "font-size:0.72rem;color:#555c6b;margin-top:8px;",
        "MEXC Perpetual: taker 0.02%, maker 0.00%, финансирование ~0.01% / 8ч. ",
        "Комиссии: 4 заполнения (2 ноги × вход + выход). Измените под свой аккаунт.")
    ),
    div(style = "padding:14px 20px;border-radius:12px;border:1px solid #1c2333;background:#0f1419;margin-bottom:18px;",
      div(style = "font-size:0.9rem;font-weight:600;color:#e6edf3;margin-bottom:10px;",
        "Фильтры пар"),
      layout_columns(col_widths = c(3, 3, 3, 3),
        checkboxInput("short_coint_only", "Только коинтегрированные", value = FALSE),
        sliderInput("short_min_corr", "Мин. корреляция", min = 50, max = 100, value = 50, step = 5, post = "%", width = "100%"),
        sliderInput("short_max_days", "Макс. дней в сделке", min = 1, max = 7, value = 7, step = 1, post = " дн.", width = "100%"),
        sliderInput("short_min_trades", "Мин. сделок в истории", min = 1, max = 20, value = 3, step = 1, width = "100%")
      )
    ),
    uiOutput("shorttrades_ui")
  ),

  # ── TAB 7: Сканеры ──────────────────────────────────────────────────────
  nav_panel("🔍 Сканеры",
    div(style = "padding:16px 20px;border-radius:12px;border:1px solid #1c2333;background:#0f1419;margin-bottom:18px;",
      div(style = "font-size:0.9rem;font-weight:600;color:#e6edf3;margin-bottom:10px;",
        "Выберите сканер:"),
      div(style = "display:flex;flex-wrap:wrap;gap:6px;",
        radioButtons("scanner_type", NULL,
          choices = c("🔗 Corr Breakdown" = "corrbreak", "🚀 Momentum" = "momentum",
                      "📉 Drawdown" = "drawdown"),
          selected = "corrbreak", inline = TRUE,
          width = "100%")
      )
    ),
    div(style = "padding:16px 20px;border-radius:12px;border:1px solid #1c2333;background:#0f1419;margin-bottom:18px;",
      div(style = "font-size:0.9rem;font-weight:600;color:#e6edf3;margin-bottom:12px;",
        "Настройки калькулятора (применяются ко всем сигналам)"),
      calc_settings_ui("scancalc_"),
      div(style = "font-size:0.72rem;color:#555c6b;margin-top:8px;",
        "MEXC Perpetual: taker 0.02%, maker 0.00%, финансирование ~0.01% / 8ч. ",
        "Комиссии: 4 заполнения (2 ноги × вход + выход). Измените под свой аккаунт.")
    ),
    uiOutput("scanner_ui")
  ),

  # ── TAB 8: AI-аналитик ──────────────────────────────────────────────────
  nav_panel("🤖 AI-аналитик",
    div(style = "padding:16px 20px;border-radius:12px;border:1px solid #1c2333;background:#0f1419;margin-bottom:18px;",
      div(style = "display:flex;align-items:center;gap:12px;margin-bottom:12px;",
        tags$span(style = "font-size:1.5rem;", "🤖"),
        div(
          div(style = "font-size:1.1rem;font-weight:700;color:#e6edf3;", "AI-аналитик (DeepSeek v4 Pro)"),
          div(style = "font-size:0.78rem;color:#555c6b;", "Анализирует текущие сигналы, сканеры и рынок — даёт конкретные рекомендации"))),
      actionButton("ai_analyze", "🧠 Спросить AI",
        class = "btn-primary", style = "width:100%;font-size:1rem;padding:14px;"),
      div(style = "font-size:0.72rem;color:#555c6b;margin-top:8px;",
        "AI получит: активные сигналы, рынок, волатильность, лучшие пары. Ответ — конкретные сделки с входом/выходом/размером.")),
    uiOutput("ai_result_ui")
  ),

  # ── TAB 9: Избранное ────────────────────────────────────────────────────
  nav_panel("⭐ Избранное",
    uiOutput("favorites_ui")
  )
)

# ── Server ───────────────────────────────────────────────────────────────────
server <- function(input, output, session) {

  # ══════════════════════════════════════════════════════════════════════════
  # Supabase Auth (REST API via httr)
  # ══════════════════════════════════════════════════════════════════════════
  AUTH <- reactiveValues(user_id = NULL, email = NULL, token = NULL, error = NULL)
  supabase_url <- Sys.getenv("SUPABASE_URL", "")
  supabase_key <- Sys.getenv("SUPABASE_ANON_KEY", "")

  # Login form (modal)
  observeEvent(input$show_auth, {
    showModal(modalDialog(
      title = div(style = "text-align:center;", tags$span(style = "font-size:1.5rem;", "🔐"),
                  tags$br(), "Вход / Регистрация"),
      textInput("auth_email", "Email", placeholder = "user@example.com"),
      passwordInput("auth_password", "Пароль", placeholder = "минимум 6 символов"),
      div(style = "display:flex;gap:10px;",
        actionButton("auth_login", "Войти", class = "btn-primary", width = "100%"),
        actionButton("auth_register", "Регистрация", class = "btn-secondary", width = "100%")),
      if (!is.null(AUTH$error)) div(style = paste0("color:", RED, ";font-size:0.85rem;margin-top:10px;text-align:center;"),
        AUTH$error),
      footer = NULL, size = "s", easyClose = FALSE
    ))
  })

  # Register handler
  observeEvent(input$auth_register, {
    req(input$auth_email, input$auth_password)
    if (nchar(input$auth_password) < 6) {
      AUTH$error <- "Пароль должен быть не менее 6 символов"
      return()
    }
    resp <- tryCatch(
      httr::POST(
        paste0(supabase_url, "/auth/v1/signup"),
        httr::add_headers("apikey" = supabase_key, "Content-Type" = "application/json"),
        body = jsonlite::toJSON(list(email = input$auth_email, password = input$auth_password), auto_unbox = TRUE),
        encode = "raw"
      ),
      error = function(e) NULL
    )
    if (is.null(resp) || httr::status_code(resp) >= 400) {
      AUTH$error <- "Ошибка регистрации. Проверьте email и пароль."
      return()
    }
    result <- httr::content(resp, "parsed")
    if (!is.null(result$user)) {
      AUTH$user_id <- result$user$id
      AUTH$email <- result$user$email
      AUTH$token <- result$access_token
      AUTH$error <- NULL
      removeModal()
      showNotification(paste0("Добро пожаловать, ", AUTH$email, "!"), type = "message", duration = 5)
    }
  })

  # Login handler
  observeEvent(input$auth_login, {
    req(input$auth_email, input$auth_password)
    resp <- tryCatch(
      httr::POST(
        paste0(supabase_url, "/auth/v1/token?grant_type=password"),
        httr::add_headers("apikey" = supabase_key, "Content-Type" = "application/json"),
        body = jsonlite::toJSON(list(email = input$auth_email, password = input$auth_password), auto_unbox = TRUE),
        encode = "raw"
      ),
      error = function(e) NULL
    )
    if (is.null(resp) || httr::status_code(resp) >= 400) {
      AUTH$error <- "Неверный email или пароль"
      return()
    }
    result <- httr::content(resp, "parsed")
    AUTH$user_id <- result$user$id
    AUTH$email <- result$user$email
    AUTH$token <- result$access_token
    AUTH$error <- NULL
    removeModal()
    showNotification(paste0("С возвращением, ", AUTH$email, "!"), type = "message", duration = 5)
  })

  # Logout handler
  observeEvent(input$auth_logout, {
    AUTH$user_id <- NULL; AUTH$email <- NULL; AUTH$token <- NULL
    showNotification("Вы вышли из аккаунта", type = "message")
  })

  # Show auth modal if not logged in and no Supabase configured
  observe({
    if (is.null(AUTH$user_id) && nchar(supabase_url) < 5)
      AUTH$email <- "local"
  })

  # Auth status UI (in navbar)
  output$auth_status_ui <- renderUI({
    if (!is.null(AUTH$email) && AUTH$email != "local") {
      div(style = "display:flex;align-items:center;gap:10px;",
        tags$span(style = "font-size:0.82rem;color:#8b949e;", AUTH$email),
        actionButton("auth_logout", "Выйти", class = "btn-secondary btn-sm", style = "font-size:0.75rem;"))
    } else {
      actionButton("show_auth", "🔐 Войти / Регистрация",
        class = "btn-secondary btn-sm", style = "font-size:0.82rem;")
    }
  })

  # ── Database connection ──────────────────────────────────────────────────
  DB_PATH <- Sys.getenv("DB_PATH", "/data/market.db")
  db_available <- reactive({ file.exists(DB_PATH) })

  get_db_data <- function(market = NULL) {
    if (!file.exists(DB_PATH)) return(NULL)
    con <- dbConnect(SQLite(), DB_PATH)
    on.exit(dbDisconnect(con))
    query <- if (!is.null(market)) {
      sprintf("SELECT ticker, date, close FROM prices WHERE market = '%s' ORDER BY ticker, date", market)
    } else {
      "SELECT ticker, date, close FROM prices ORDER BY ticker, date"
    }
    dbGetQuery(con, query)
  }

  get_hourly_data <- function(ticker = NULL) {
    if (!file.exists(DB_PATH)) return(NULL)
    con <- dbConnect(SQLite(), DB_PATH)
    on.exit(dbDisconnect(con))
    tables <- dbGetQuery(con, "SELECT name FROM sqlite_master WHERE type='table' AND name='hourly_prices'")$name
    if (!"hourly_prices" %in% tables) return(NULL)
    # Only select needed columns to reduce memory/transfer
    query <- if (!is.null(ticker)) {
      sprintf("SELECT ticker, hour, close, high, low FROM hourly_prices WHERE ticker = '%s' ORDER BY timestamp", ticker)
    } else {
      "SELECT ticker, hour, close, high, low FROM hourly_prices ORDER BY ticker, timestamp"
    }
    dbGetQuery(con, query)
  }

  # ── DB status (shown in Данные tab) ─────────────────────────────────────
  output$db_status_ui <- renderUI({
    if (!db_available()) {
      return(div(style = "padding:16px;border-radius:10px;border:1px solid #f85149;background:#1a0d0d;",
        tags$span(style = "color:#f85149;font-weight:600;", "⚠ База данных не найдена"),
        tags$br(),
        tags$code(style = "color:#8b949e;font-size:0.8rem;", DB_PATH)
      ))
    }
    con <- dbConnect(SQLite(), DB_PATH)
    on.exit(dbDisconnect(con))
    stats <- tryCatch(
      dbGetQuery(con, "SELECT COUNT(DISTINCT ticker) AS n_tickers, COUNT(*) AS n_rows, MIN(date) AS min_d, MAX(date) AS max_d FROM prices"),
      error = function(e) NULL
    )
    if (is.null(stats) || nrow(stats) == 0 || stats$n_rows == 0) {
      return(div(style = "padding:16px;border-radius:10px;border:1px solid #f85149;background:#1a0d0d;",
        tags$span(style = "color:#f85149;font-weight:600;", "⚠ БД пуста или повреждена")
      ))
    }

    # Last data update (from update_log)
    last_update <- tryCatch({
      r <- dbGetQuery(con, "SELECT timestamp, market, rows_added, status FROM update_log ORDER BY timestamp DESC LIMIT 1")
      if (nrow(r) > 0) r else NULL
    }, error = function(e) NULL)

    # Last analysis computation (from pairs.computed_at)
    last_analysis <- tryCatch({
      r <- dbGetQuery(con, "SELECT computed_at FROM pairs ORDER BY computed_at DESC LIMIT 1")
      if (nrow(r) > 0 && !is.na(r$computed_at[1])) r$computed_at[1] else NULL
    }, error = function(e) NULL)

    # Format timestamps nicely
    fmt_ts <- function(ts) {
      if (is.null(ts) || is.na(ts)) return("—")
      # Parse and format: "2026-06-24 06:00:12" -> "24.06 09:00 МСК"
      tryCatch({
        dt <- as.POSIXct(ts, tz = "UTC")
        msk <- format(as.POSIXct(dt, tz = "UTC"), tz = "Europe/Moscow", "%d.%m %H:%M")
        paste0(msk, " МСК")
      }, error = function(e) as.character(ts))
    }

    # Next update time
    now <- Sys.time()
    next_update <- tryCatch({
      today_6utc <- as.POSIXct(paste(format(as.Date(now), "%Y-%m-%d"), "06:00:00"), tz = "UTC")
      if (now < today_6utc) today_6utc else today_6utc + 86400
    }, error = function(e) NULL)
    next_txt <- if (!is.null(next_update))
      paste0(format(as.POSIXct(next_update, tz = "UTC"), tz = "Europe/Moscow", "%d.%m %H:%M"), " МСК")
      else "—"

    # Update status (fresh or stale)
    is_fresh <- !is.null(last_update) && !is.na(last_update$timestamp[1]) &&
      (as.numeric(difftime(now, as.POSIXct(last_update$timestamp[1], tz = "UTC"), units = "hours")) < 26)
    update_col <- if (is_fresh) "#3fb950" else "#f7931a"
    update_icon <- if (is_fresh) "✓" else "⚠"

    div(style = "padding:14px 16px;border-radius:10px;border:1px solid #30363d;background:#0d1117;",
      tags$span(style = "color:#3fb950;font-weight:600;", "✓ БД подключена"),
      tags$br(),
      tags$span(style = "color:#adbac7;font-size:0.88rem;",
        paste0(stats$n_tickers, " тикеров · ",
               format(stats$n_rows, big.mark = " "), " записей · ",
               stats$min_d, " — ", stats$max_d)),
      tags$br(),
      tags$code(style = "color:#555;font-size:0.75rem;", DB_PATH),
      tags$hr(style = "border-color:#30363d;margin:10px 0;"),
      # Last update
      tags$div(style = "display:flex;align-items:center;gap:8px;margin-bottom:6px;",
        tags$span(style = paste0("color:", update_col, ";font-size:0.82rem;font-weight:600;"),
          paste0(update_icon, " Данные обновлены:")),
        tags$span(style = "color:#adbac7;font-size:0.82rem;",
          fmt_ts(last_update$timestamp[1])),
        if (!is.null(last_update) && !is.na(last_update$rows_added[1]))
          tags$span(style = "color:#555;font-size:0.72rem;",
            paste0("(+", last_update$rows_added[1], " строк)"))),
      # Last analysis
      tags$div(style = "display:flex;align-items:center;gap:8px;margin-bottom:6px;",
        tags$span(style = "color:#3fb950;font-size:0.82rem;font-weight:600;",
          "✓ Анализ пересчитан:"),
        tags$span(style = "color:#adbac7;font-size:0.82rem;",
          fmt_ts(last_analysis))),
      # Next update
      tags$div(style = "display:flex;align-items:center;gap:8px;",
        tags$span(style = "color:#58a6ff;font-size:0.82rem;font-weight:600;",
          "⏰ Следующее обновление:"),
        tags$span(style = "color:#adbac7;font-size:0.82rem;", next_txt))
    )
  })

  # ── DB pairs reader (precomputed analysis) ──────────────────────────────
  get_db_pairs <- function(market) {
    if (!file.exists(DB_PATH)) return(NULL)
    con <- dbConnect(SQLite(), DB_PATH)
    on.exit(dbDisconnect(con))
    tables <- dbGetQuery(con, "SELECT name FROM sqlite_master WHERE type='table' AND name='pairs'")$name
    if (!"pairs" %in% tables) return(NULL)
    dbGetQuery(con, "SELECT * FROM pairs WHERE market = ? ORDER BY score DESC",
               params = list(market))
  }

  # ── Unified data source: SQLite DB ────────────────────────────────────────
  raw_data <- reactive({
    if (!db_available()) return(NULL)
    mt <- input$market_type
    db_df <- get_db_data(mt)
    if (is.null(db_df) || nrow(db_df) == 0) return(NULL)
    db_df$ticker_col <- db_df$ticker
    db_df$price_col  <- as.numeric(db_df$close)
    db_df$date       <- as.Date(db_df$date)
    db_df <- db_df[!is.na(db_df$date) & !is.na(db_df$price_col) & db_df$price_col > 0, ]
    if (nrow(db_df) == 0) return(NULL)
    db_df[order(db_df$ticker_col, db_df$date), ]
  })

  # ── Auto-analysis status (shown in Данные tab) ───────────────────────────
  output$analysis_status_ui <- renderUI({
    req(input$market_type)
    df <- pairs_coint()
    if (is.null(df) || nrow(df) == 0) {
      return(div(style = "padding:14px 16px;border-radius:10px;border:1px solid #f85149;background:#1a0d0d;",
        tags$span(style = "color:#f85149;font-weight:600;", "⚠ Анализ не рассчитан"),
        tags$br(),
        tags$span(style = "color:#8b949e;font-size:0.82rem;",
          "Запустите: Rscript /scripts/compute_analysis.R")
      ))
    }
    n_active <- sum(df$signal_type != "wait")
    div(style = "padding:14px 16px;border-radius:10px;border:1px solid #30363d;background:#0d1117;",
      tags$span(style = "color:#3fb950;font-weight:600;", "✓ Анализ готов"),
      tags$br(),
      tags$span(style = "color:#adbac7;font-size:0.88rem;",
        paste0(nrow(df), " пар · ",
               sum(df$is_coint), " коинтегрированных · ",
               n_active, " активных сигналов")),
      tags$br(),
      tags$span(style = "color:#555;font-size:0.75rem;",
        "Перерасчёт — автоматически каждый день в 09:00 MSK (cron)")
    )
  })

  output$data_summary <- renderUI({
    df <- raw_data()
    if (is.null(df)) return(div(
      style = "text-align:center;padding:40px;color:#555;",
      tags$i(class = "fas fa-database fa-3x",
             style = "display:block;margin-bottom:12px;color:#30363d;"),
      p("Нет данных по этому рынку")
    ))
    layout_columns(col_widths = c(4,4,4),
      value_box("Инструментов", length(unique(df$ticker_col)),
                showcase = icon("chart-line"), theme = "primary"),
      value_box("Записей", format(nrow(df), big.mark = " "),
                showcase = icon("database"), theme = "secondary"),
      value_box("Период",  paste(format(min(df$date), "%d.%m.%y"),
                                 "–", format(max(df$date), "%d.%m.%y")),
                showcase = icon("calendar"), theme = "secondary")
    )
  })

  output$preview_table <- renderDT({
    df <- raw_data(); req(df)
    show <- df[, c("ticker_col", "date", "price_col"), drop = FALSE]
    colnames(show) <- c("Тикер", "Дата", "Цена закрытия")
    datatable(head(show, 300),
              options = list(pageLength = 10, scrollX = TRUE, dom = "tip"),
              style = "bootstrap5", class = "table-dark table-sm")
  })

  # ── Precomputed pairs (read from DB) ──────────────────────────────────────
  pairs_coint <- reactive({
    req(input$market_type)
    df <- get_db_pairs(input$market_type)
    if (is.null(df) || nrow(df) == 0) return(NULL)
    data.frame(
      A           = df$ticker_a,
      B           = df$ticker_b,
      corr        = df$corr,
      halflife    = df$halflife,
      t_stat      = df$t_stat,
      is_coint    = as.logical(df$is_coint),
      hedge_ratio = df$hedge_ratio,
      score       = df$score,
      z_now       = df$z_now,
      z_forecast  = df$z_forecast,
      signal      = df$signal,
      signal_type = df$signal_type,
      strength    = df$strength,
      stringsAsFactors = FALSE
    )
  })

  # ── Price matrix (for spread chart + backtest, reactive on market) ────────
  price_wide <- reactive({
    df <- raw_data(); req(df)
    pw <- df |>
      select(ticker_col, date, price_col) |>
      pivot_wider(names_from = ticker_col, values_from = price_col, values_fn = mean) |>
      arrange(date)
    dates <- pw$date
    mat <- as.data.frame(lapply(pw[, -1, drop = FALSE], as.numeric))
    rownames(mat) <- as.character(dates)
    mat
  })

  # ── Signals (filter precomputed pairs) ────────────────────────────────────
  signals_data <- reactive({
    df <- pairs_coint()
    if (is.null(df)) return(data.frame())
    good <- df[!is.na(df$corr) & abs(df$corr) >= 0.5, , drop = FALSE]
    if (nrow(good) == 0) return(data.frame())
    good$corr <- round(abs(good$corr) * 100)
    good
  })

  # ── ТАБ: Pairs Trading ────────────────────────────────────────────────────
  output$pairs_ui <- renderUI({
    df <- pairs_coint()
    if (is.null(df) || nrow(df) == 0) return(placeholder_msg("Анализ не рассчитан. Проверьте БД."))
    tagList(
      card(
        card_header("🤝 Лучшие пары для pairs trading"),
        card_body(
          p(style = "color:#8b949e;font-size:0.85rem;",
            "Pairs trading — стратегия: покупаем отстающий инструмент и продаём опередивший, ",
            "ждём возврата к средней. Нужны два инструмента, которые: (1) сильно коррелируют ",
            "и (2) образуют стабильный спред (коинтегрированы). Полутора-двухмесячный полупериод — идеален."),
          uiOutput("pairs_cards")
        )
      ),
      card(
        card_header(
          layout_columns(col_widths = c(8, 4),
            "Все пары — детальная таблица",
            div(style = "text-align:right;",
              downloadButton("dl_pairs_csv", "⬇ Скачать все пары (CSV)",
                             class = "btn-sm btn-secondary"))
          )
        ),
        card_body(
          p(style = "color:#8b949e;font-size:0.82rem;",
            "Полупериод: за сколько дней спред возвращается к среднему. 5–30 дней — лучший диапазон для трейдинга."),
          DTOutput("pairs_table")
        )
      ),
      card(
        card_header(
          layout_columns(col_widths = c(8, 4),
            "📉 График спреда для выбранной пары",
            div(style = "text-align:right;",
              downloadButton("dl_spread_csv", "⬇ Скачать Z-score (CSV)",
                             class = "btn-sm btn-secondary"))
          )
        ),
        card_body(
          uiOutput("spread_pair_selector"),
          plotOutput("spread_chart", height = "360px"),
          uiOutput("spread_explanation")
        )
      ),
      card(
        card_header("📊 Исторические сигналы и ожидаемый P&L"),
        card_body(uiOutput("backtest_ui"))
      )
    )
  })

  output$pairs_cards <- renderUI({
    df <- pairs_coint(); req(df)
    good <- df[!is.na(df$corr) & abs(df$corr) >= 0.5, ]
    top  <- head(good, 6)
    if (nrow(top) == 0) {
      return(div(style = "text-align:center;padding:30px;color:#555;",
        p("Не найдено пар с достаточной корреляцией (>50%). Попробуйте добавить больше тикеров.")))
    }
    rows <- lapply(seq_len(nrow(top)), function(i) {
      r  <- top[i, ]
      corr_col <- dot_color(r$corr)
      hl_col   <- halflife_color(r$halflife)
      hl_lbl   <- halflife_label(r$halflife)
      coint_txt <- if (r$is_coint) "✅ Коинтегрированы" else "⚠️ Не коинтегрированы"
      coint_col <- if (r$is_coint) GREEN else ORANGE
      tags$div(style = paste0(
        "border:1px solid ", if (r$is_coint) GREEN else BORDER,
        ";border-radius:10px;padding:16px 18px;margin-bottom:12px;background:", BG, ";"),
        layout_columns(col_widths = c(7, 5),
          div(
            tags$span(style = "font-size:1.05rem;font-weight:700;color:#e6edf3;",
              r$A, " ↔ ", r$B),
            tags$br(), tags$br(),
            tags$span(style = paste0("font-size:0.85rem;color:", coint_col, ";font-weight:600;"),
              coint_txt),
            tags$br(),
            tags$span(style = "font-size:0.82rem;color:#8b949e;",
              paste0("Синхронность движений: ", round(abs(r$corr)*100), "%"))
          ),
          div(style = "text-align:right;",
            badge(paste0("Синхр. ", round(abs(r$corr)*100), "%"), corr_col),
            tags$br(), tags$br(),
            if (!is.na(r$halflife))
              badge(hl_lbl, hl_col)
            else
              badge("Полупериод: нет данных", GRAY)
            )
          )
        )
      })
    tagList(tagList(rows))
  })

  output$pairs_table <- renderDT({
    df <- pairs_coint(); req(df)
    out <- data.frame(
      "A"               = df$A,
      "B"               = df$B,
      "Синхронность"    = paste0(round(abs(df$corr) * 100, 1), "%"),
      "Коинтеграция"    = ifelse(df$is_coint, "✅ Да", "—"),
      "Полупериод (дн.)"= ifelse(is.na(df$halflife), "—", as.character(df$halflife)),
      "Рейтинг"         = round(df$score, 2),
      stringsAsFactors  = FALSE, check.names = FALSE
    )
    datatable(out, rownames = FALSE,
              options = list(pageLength = 20, dom = "tip", scrollX = TRUE,
                             order = list(list(5, "desc"))),
              style = "bootstrap5", class = "table-dark table-sm")
  })

  output$dl_pairs_csv <- downloadHandler(
    filename = function() paste0("pairs_analysis_", Sys.Date(), ".csv"),
    content  = function(file) {
      df <- pairs_coint(); req(df)
      out <- data.frame(
        ticker_a         = df$A,
        ticker_b         = df$B,
        correlation_pct  = round(abs(df$corr) * 100, 1),
        cointegrated     = ifelse(df$is_coint, "yes", "no"),
        halflife_days    = ifelse(is.na(df$halflife), NA_integer_, df$halflife),
        hedge_ratio      = round(df$hedge_ratio, 4),
        adf_t_stat       = round(df$t_stat, 3),
        score            = round(df$score, 3),
        stringsAsFactors = FALSE
      )
      write.csv(out, file, row.names = FALSE)
    }
  )

  output$dl_spread_csv <- downloadHandler(
    filename = function() {
      ta <- if (isTruthy(input$spread_ticker_a)) input$spread_ticker_a else "A"
      tb <- if (isTruthy(input$spread_ticker_b)) input$spread_ticker_b else "B"
      paste0("spread_zscore_", ta, "_", tb, "_", Sys.Date(), ".csv")
    },
    content = function(file) {
      s <- spread_data(); req(s)
      out <- data.frame(
        date            = s$dates,
        spread_log      = round(s$spread,  6),
        zscore          = round(s$zscore,  4),
        stringsAsFactors = FALSE
      )
      write.csv(out, file, row.names = FALSE)
    }
  )

  # ── Backtest ──────────────────────────────────────────────────────────────
  backtest_trades <- reactive({
    s <- spread_data(); req(s)
    z      <- s$zscore
    dates  <- s$dates
    spread <- s$spread
    n      <- length(z)
    entry_z  <- 2.0   # entry threshold
    exit_z   <- 0.5   # close threshold
    stop_z   <- 3.5   # stop-loss threshold

    trades <- list()
    in_trade <- FALSE
    entry_idx <- NA; entry_dir <- NA; entry_spread <- NA

    for (i in seq_len(n)) {
      zi <- z[i]; req(!is.na(zi))
      if (is.na(zi)) next
      if (!in_trade) {
        if (zi >=  entry_z) { in_trade <- TRUE; entry_dir <- -1; entry_idx <- i; entry_spread <- spread[i] }
        if (zi <= -entry_z) { in_trade <- TRUE; entry_dir <-  1; entry_idx <- i; entry_spread <- spread[i] }
      } else {
        # Stop-loss check
        hit_stop <- (entry_dir == -1 && zi >= stop_z) || (entry_dir == 1 && zi <= -stop_z)
        # Take profit check
        hit_tp   <- abs(zi) <= exit_z
        if (hit_stop || hit_tp) {
          pnl_log  <- entry_dir * (spread[i] - entry_spread)  # profit in log-return terms
          pnl_pct  <- (exp(pnl_log) - 1) * 100               # convert to %
          hold     <- as.integer(dates[i] - dates[entry_idx])
          trades[[length(trades) + 1]] <- data.frame(
            entry_date   = format(dates[entry_idx], "%Y-%m-%d"),
            exit_date    = format(dates[i],          "%Y-%m-%d"),
            direction    = if (entry_dir == -1) paste0("Шорт ", s$row$A, " / Лонг ", s$row$B)
                           else                  paste0("Лонг ", s$row$A, " / Шорт ", s$row$B),
            entry_z      = round(z[entry_idx], 2),
            exit_z       = round(zi, 2),
            hold_days    = hold,
            pnl_pct      = round(pnl_pct, 2),
            result       = if (hit_stop) "Стоп-лосс" else "Тейк-профит",
            stringsAsFactors = FALSE
          )
          in_trade <- FALSE
        }
      }
    }
    if (length(trades) == 0) return(data.frame())
    do.call(rbind, trades)
  })

  output$backtest_ui <- renderUI({
    s <- spread_data(); req(s)
    z_now <- tail(s$zscore, 1)

    # ── Forecast block ──────────────────────────────────────────────────────
    fc <- s$forecast
    forecast_block <- if (!is.null(fc)) {
      zh    <- round(fc$z_hat, 2)
      lo80  <- round(fc$lo80, 2); hi80 <- round(fc$hi80, 2)
      lo95  <- round(fc$lo95, 2); hi95 <- round(fc$hi95, 2)
      p_sig <- round((fc$p_long + fc$p_short) * 100)
      p_lng <- round(fc$p_long  * 100)
      p_sht <- round(fc$p_short * 100)
      reversion_speed <- if (!is.na(fc$ar_b) && fc$ar_b < 0)
        paste0(round((1 - abs(fc$ar_b)) * 100), "% от текущего отклонения")
        else "Слабый возврат к среднему"

      # Color based on forecast direction
      fc_col <- if (zh > 1.5) RED else if (zh < -1.5) BLUE else GREEN
      arrow  <- if (zh > z_now + 0.1) "↑" else if (zh < z_now - 0.1) "↓" else "→"

      # Signal probability sentence
      sig_txt <- if (p_sig < 5) {
        "Сигнала завтра, скорее всего, не будет"
      } else if (p_lng > p_sht) {
        paste0("Вероятность сигнала «Лонг ", s$row$A, "»: ", p_lng, "%")
      } else {
        paste0("Вероятность сигнала «Лонг ", s$row$B, "»: ", p_sht, "%")
      }

      div(style = paste0(
        "padding:16px 18px;border-radius:12px;border:2px solid ", BLUE,
        ";background:#0d1b2a;margin-bottom:18px;"),
        tags$b(style = paste0("color:", BLUE, ";font-size:1rem;"),
               "🔮 Прогноз Z-score на следующий день"),
        br(), br(),
        layout_columns(col_widths = c(4, 4, 4),
          # Point estimate
          div(style = "text-align:center;",
            div(style = "font-size:0.8rem;color:#8b949e;", "Ожидаемый Z завтра"),
            div(style = paste0("font-size:2rem;font-weight:800;color:", fc_col, ";"),
              paste0(arrow, " ", zh)),
            div(style = "font-size:0.75rem;color:#8b949e;",
              paste0("Сегодня: ", round(z_now, 2)))
          ),
          # Intervals
          div(style = "text-align:center;",
            div(style = "font-size:0.8rem;color:#8b949e;", "Вероятный диапазон"),
            div(style = "font-size:1rem;font-weight:700;color:#e6edf3;margin-top:4px;",
              paste0(lo80, " … ", hi80)),
            div(style = "font-size:0.75rem;color:#555;margin-top:2px;",
              paste0("80%: ", lo80, " / ", hi80)),
            div(style = "font-size:0.75rem;color:#555;",
              paste0("95%: ", lo95, " / ", hi95))
          ),
          # Signal probability
          div(style = "text-align:center;",
            div(style = "font-size:0.8rem;color:#8b949e;", "Вер-ть нового сигнала"),
            div(style = paste0("font-size:2rem;font-weight:800;color:",
                               if (p_sig >= 20) ORANGE else GREEN, ";"),
              paste0(p_sig, "%")),
            div(style = "font-size:0.75rem;color:#8b949e;", sig_txt)
          )
        ),
        br(),
        div(style = "font-size:0.8rem;color:#555;border-top:1px solid #30363d;padding-top:8px;",
          paste0("Модель: AR(1) на Z-score. Скорость возврата к 0: ", reversion_speed, ". ",
                 "Прогноз статистический — не финансовый совет."))
      )
    } else {
      div(style = "color:#555;font-size:0.85rem;margin-bottom:16px;",
          "Недостаточно данных для прогноза")
    }

    # Current signal block
    entry_z <- 2.0; exit_z <- 0.5
    if (!is.na(z_now) && abs(z_now) >= entry_z) {
      dir_txt <- if (z_now > 0) paste0("Шорт ", s$row$A, " / Лонг ", s$row$B)
                 else            paste0("Лонг ", s$row$A, " / Шорт ", s$row$B)
      entry_block <- div(style = paste0(
        "padding:14px 18px;border-radius:10px;border:2px solid ", GREEN,
        ";background:#0f2a1a;margin-bottom:16px;"),
        tags$b(style = paste0("color:", GREEN, ";font-size:1rem;"), "🟢 АКТИВНЫЙ СИГНАЛ ВХОДА"),
        tags$br(),
        tags$span(style = "color:#e6edf3;", dir_txt),
        tags$br(),
        tags$span(style = "color:#8b949e;font-size:0.85rem;",
          paste0("Текущий Z = ", round(z_now, 2),
                 " | Выходить когда |Z| < ", exit_z))
      )
    } else if (!is.na(z_now) && abs(z_now) >= 1.0) {
      entry_block <- div(style = paste0(
        "padding:14px 18px;border-radius:10px;border:2px solid ", ORANGE,
        ";background:#1a1400;margin-bottom:16px;"),
        tags$b(style = paste0("color:", ORANGE, ";"), "🟡 Сигнала нет — наблюдать"),
        tags$br(),
        tags$span(style = "color:#8b949e;font-size:0.85rem;",
          paste0("Z = ", round(z_now, 2), " — ждём пересечения ±2.0 для входа"))
      )
    } else {
      entry_block <- div(style = paste0(
        "padding:14px 18px;border-radius:10px;border:1px solid ", BORDER,
        ";background:", BG, ";margin-bottom:16px;"),
        tags$b(style = "color:#8b949e;", "⚪ Спред у нормы — позиций нет"),
        tags$br(),
        tags$span(style = "color:#8b949e;font-size:0.85rem;",
          paste0("Z = ", round(z_now, 2), " (нужно ≥ ±2.0 для входа)"))
      )
    }

    # Stats block
    tr <- backtest_trades()
    if (nrow(tr) == 0) {
      stats_block <- p(style = "color:#555;", "Не было сигналов с |Z| ≥ 2.0 за выбранный период.")
    } else {
      wins    <- tr[tr$pnl_pct > 0, ]
      losses  <- tr[tr$pnl_pct <= 0, ]
      avg_win  <- if (nrow(wins)   > 0) mean(wins$pnl_pct)   else 0
      avg_loss <- if (nrow(losses) > 0) mean(losses$pnl_pct) else 0
      win_rate <- round(nrow(wins) / nrow(tr) * 100)
      avg_hold <- round(mean(tr$hold_days))
      avg_pnl  <- round(mean(tr$pnl_pct), 2)
      stat_col <- if (avg_pnl > 0) GREEN else RED

      stats_block <- tagList(
        layout_columns(col_widths = c(3,3,3,3),
          div(style = paste0("text-align:center;padding:12px;border-radius:8px;border:1px solid ",
                             BORDER, ";background:", BG, ";"),
            div(style = "font-size:0.8rem;color:#8b949e;", "Всего сделок"),
            div(style = "font-size:1.5rem;font-weight:700;color:#e6edf3;", nrow(tr))
          ),
          div(style = paste0("text-align:center;padding:12px;border-radius:8px;border:1px solid ",
                             BORDER, ";background:", BG, ";"),
            div(style = "font-size:0.8rem;color:#8b949e;", "Прибыльных"),
            div(style = paste0("font-size:1.5rem;font-weight:700;color:",
                               if (win_rate >= 50) GREEN else RED, ";"), paste0(win_rate, "%"))
          ),
          div(style = paste0("text-align:center;padding:12px;border-radius:8px;border:1px solid ",
                             BORDER, ";background:", BG, ";"),
            div(style = "font-size:0.8rem;color:#8b949e;", "Средний P&L"),
            div(style = paste0("font-size:1.5rem;font-weight:700;color:", stat_col, ";"),
              paste0(if (avg_pnl > 0) "+" else "", avg_pnl, "%"))
          ),
          div(style = paste0("text-align:center;padding:12px;border-radius:8px;border:1px solid ",
                             BORDER, ";background:", BG, ";"),
            div(style = "font-size:0.8rem;color:#8b949e;", "Ср. удержание"),
            div(style = "font-size:1.5rem;font-weight:700;color:#e6edf3;",
              paste0(avg_hold, " дн."))
          )
        ),
        br(),
        p(style = "color:#8b949e;font-size:0.82rem;",
          paste0("Прибыльные сделки: средний +", round(avg_win, 1), "% | ",
                 "Убыточные: средний ", round(avg_loss, 1), "% | ",
                 "Вход: |Z| ≥ 2.0, выход: |Z| < 0.5 или стоп |Z| ≥ 3.5")),
        br(),
        # Last trades table
        tags$b(style = "color:#adbac7;", "Последние сделки:"),
        br(), br(),
        DTOutput("trades_table")
      )
    }

    tagList(forecast_block, entry_block, stats_block)
  })

  output$trades_table <- renderDT({
    tr <- backtest_trades(); req(nrow(tr) > 0)
    out <- tr[, c("entry_date","exit_date","direction","entry_z","exit_z","hold_days","pnl_pct","result")]
    colnames(out) <- c("Вход (дата)","Выход (дата)","Направление","Z входа","Z выхода",
                       "Дней","P&L %","Итог")
    datatable(out, rownames = FALSE,
              options = list(pageLength = 10, dom = "tip", scrollX = TRUE,
                             order = list(list(0, "desc"))),
              style = "bootstrap5", class = "table-dark table-sm") |>
      formatStyle("P&L %",
        color = styleInterval(0, c("#f85149", "#3fb950")),
        fontWeight = "bold")
  })

  output$spread_pair_selector <- renderUI({
    df <- pairs_coint(); req(df)
    tickers <- sort(unique(c(df$A, df$B)))
    layout_columns(col_widths = c(5, 5, 2),
      selectInput("spread_ticker_a", "Тикер A:",
                  choices = tickers, selected = tickers[1],
                  selectize = FALSE, size = 1),
      selectInput("spread_ticker_b", "Тикер B:",
                  choices = tickers, selected = tickers[min(2, length(tickers))],
                  selectize = FALSE, size = 1),
      div(style = "padding-top:28px;",
        actionButton("swap_tickers", "⇄", class = "btn-secondary w-100",
                     title = "Поменять местами"))
    )
  })

  observeEvent(input$swap_tickers, {
    a <- input$spread_ticker_a
    b <- input$spread_ticker_b
    updateSelectInput(session, "spread_ticker_a", selected = b)
    updateSelectInput(session, "spread_ticker_b", selected = a)
  })

  # Reactive: compute z-score series for selected pair
  spread_data <- reactive({
    req(input$spread_ticker_a, input$spread_ticker_b)
    ta <- input$spread_ticker_a; tb <- input$spread_ticker_b
    validate(need(ta != tb, "Выберите два разных тикера"))
    pw <- price_wide(); req(pw)
    # Normalize: pivot_wider converts '/' to '.' in column names
    ta <- if (ta %in% colnames(pw)) ta else gsub("/", ".", ta)
    tb <- if (tb %in% colnames(pw)) tb else gsub("/", ".", tb)
    validate(need(ta %in% colnames(pw), paste("Тикер", ta, "не найден в данных")))
    validate(need(tb %in% colnames(pw), paste("Тикер", tb, "не найден в данных")))
    pa <- as.numeric(pw[[ta]]); pb <- as.numeric(pw[[tb]])
    dates <- as.Date(rownames(pw))
    ok  <- !is.na(pa) & !is.na(pb) & pa > 0 & pb > 0
    validate(need(sum(ok) >= 20, "Недостаточно совместных данных для этой пары"))
    # Compute hedge ratio via OLS for this specific pair
    cg <- engle_granger(pa, pb)
    hr <- if (!is.na(cg$hedge_ratio)) cg$hedge_ratio else 1
    spread <- log(pa[ok]) - hr * log(pb[ok])
    mn  <- mean(spread, na.rm = TRUE)
    sd1 <- sd(spread, na.rm = TRUE)
    zscore <- if (sd1 > 0) (spread - mn) / sd1 else rep(0, length(spread))
    fake_row <- data.frame(A = ta, B = tb, halflife = cg$halflife,
                           is_coint = cg$is_coint, hedge_ratio = hr,
                           stringsAsFactors = FALSE)

    # AR(1) one-step-ahead forecast on Z-score
    z_clean <- zscore[!is.na(zscore)]
    forecast <- tryCatch({
      n_z  <- length(z_clean)
      zy   <- z_clean[-1]
      zx   <- z_clean[-n_z]
      arft <- lm(zy ~ zx)
      cf   <- coef(arft)
      z_hat <- cf[1] + cf[2] * tail(z_clean, 1)   # E[Z_tomorrow]
      sigma <- sd(residuals(arft), na.rm = TRUE)   # noise std dev
      list(
        z_hat  = z_hat,
        lo80   = z_hat - 1.28 * sigma,
        hi80   = z_hat + 1.28 * sigma,
        lo95   = z_hat - 1.96 * sigma,
        hi95   = z_hat + 1.96 * sigma,
        # P(signal) = P(|Z_tomorrow| > 2)
        p_long  = pnorm(-2, mean = z_hat, sd = sigma),        # P(Z < -2)
        p_short = 1 - pnorm(2, mean = z_hat, sd = sigma),     # P(Z > 2)
        ar_b   = cf[2]
      )
    }, error = function(e) NULL)

    list(dates = dates[ok], spread = spread, zscore = zscore,
         mean = mn, sd = sd1, row = fake_row, forecast = forecast)
  })

  output$spread_chart <- renderPlot({
    s <- spread_data(); req(s)
    plot_df <- data.frame(date = s$dates, z = s$zscore)
    ggplot(plot_df, aes(date, z)) +
      geom_ribbon(aes(ymin = -2, ymax = 2), fill = "#1f2937", alpha = 0.5) +
      geom_ribbon(aes(ymin = -1, ymax = 1), fill = "#374151", alpha = 0.6) +
      geom_line(color = BLUE, linewidth = 0.9) +
      geom_hline(yintercept =  0, color = GRAY,   linetype = "dashed", linewidth = 0.7) +
      geom_hline(yintercept =  1, color = ORANGE, linetype = "dotted", linewidth = 0.7) +
      geom_hline(yintercept = -1, color = ORANGE, linetype = "dotted", linewidth = 0.7) +
      geom_hline(yintercept =  2, color = RED,    linetype = "solid",  linewidth = 0.8) +
      geom_hline(yintercept = -2, color = RED,    linetype = "solid",  linewidth = 0.8) +
      annotate("text", x = min(s$dates), y =  2.1, label = "+2σ (сигнал шорт A / лонг B)",
               color = RED,    size = 3, hjust = 0) +
      annotate("text", x = min(s$dates), y = -2.1, label = "-2σ (сигнал лонг A / шорт B)",
               color = RED,    size = 3, hjust = 0) +
      scale_y_continuous(breaks = c(-3,-2,-1,0,1,2,3)) +
      labs(x = NULL, y = "Z-score спреда",
           title = paste0("Z-score спреда: ", s$row$A, " / ", s$row$B),
           subtitle = paste0(
             if (!is.na(s$row$halflife)) paste0("Полупериод: ", s$row$halflife, " дн. | ") else "",
             if (s$row$is_coint) "✓ Коинтегрированы" else "Нет коинтеграции")) +
      dark_theme
  }, bg = CARD)

  output$spread_explanation <- renderUI({
    s <- spread_data(); req(s)
    z_now   <- tail(s$zscore, 1)
    z_round <- round(z_now, 2)
    signal_col <- if (abs(z_now) >= 2) RED else if (abs(z_now) >= 1) ORANGE else GREEN
    signal_txt <- if (z_now >=  2) paste0("🔴 Лонг ", s$row$B, " / Шорт ", s$row$A)
             else if (z_now <= -2) paste0("🔴 Лонг ", s$row$A, " / Шорт ", s$row$B)
             else if (z_now >=  1) paste0("🟡 Спред расширяется — наблюдать")
             else if (z_now <= -1) paste0("🟡 Спред сужается — наблюдать")
             else "🟢 Спред у нормы — позиций нет"
    tagList(
      layout_columns(col_widths = c(4, 8),
        tags$div(style = paste0(
          "text-align:center;padding:18px;border-radius:10px;",
          "border:2px solid ", signal_col, ";background:", BG, ";margin-top:12px;"),
          tags$div(style = "font-size:0.8rem;color:#8b949e;", "Текущий Z-score"),
          tags$div(style = paste0("font-size:2.2rem;font-weight:800;color:", signal_col, ";"),
            z_round),
          tags$div(style = paste0("font-size:0.85rem;font-weight:600;color:", signal_col,
                                  ";margin-top:4px;"), signal_txt)
        ),
        tags$div(style = "margin-top:12px;padding:12px 16px;border-radius:8px;background:#0d1117;",
          tags$p(style = "color:#8b949e;font-size:0.85rem;margin:0;",
            "📌 ", tags$b("Как читать: "),
            "Z-score = на сколько σ спред сейчас отклонился от среднего. ",
            tags$b("|Z| > 2"), " → сигнал на вход. ",
            tags$b("|Z| < 0.5"), " → закрыть позицию. ",
            "Серая полоса (±1σ) — норма. Красные линии (±2σ) — зона входа."
          )
        )
      )
    )
  })

  # ── ТАБ: Сигналы ──────────────────────────────────────────────────────────
  output$signals_ui <- renderUI({
    df <- pairs_coint()
    if (is.null(df) || nrow(df) == 0) return(placeholder_msg("Анализ не рассчитан. Проверьте БД."))
    mode <- if (isTruthy(input$signals_mode)) input$signals_mode else "all"

    if (mode == "all") {
      # Existing signals view
      tagList(
        card(
          card_header("🚦 Торговые сигналы на завтра"),
          card_body(
            p(style = "color:#8b949e;font-size:0.85rem;",
              "Сигналы формируются на основе Z-score спреда коинтегрированных пар. ",
              "Вход при |Z| > 2, выход при |Z| < 0.5. Прогноз — AR(1) модель."),
            checkboxInput("signals_coint_only", "Только коинтегрированные пары", value = TRUE),
            sliderInput("signals_min_corr", "Мин. корреляция", min = 50, max = 100, value = 70, step = 5, post = "%", width = "100%"),
            uiOutput("signals_active"),
            hr(),
            tags$h6(style = "color:#e6edf3;margin-top:16px;", "📋 Все пары — сводная таблица"),
            DTOutput("signals_table")
          )
        )
      )
    } else if (mode == "forecast") {
      tdf <- forecast_trades()
      if (is.null(tdf) || nrow(tdf) == 0)
        return(placeholder_msg("Нет активных сигналов для прогноза."))
      tagList(
        card(card_header(paste0("💎 Прогноз: ", nrow(tdf), " активных сигналов")),
          card_body(DTOutput("maxprofit_table")))
      )
    } else if (mode == "short") {
      tdf <- shortforecast_data()
      md <- if (isTruthy(input$short_max_days)) input$short_max_days else 7
      if (is.null(tdf) || nrow(tdf) == 0)
        return(placeholder_msg(sprintf("Нет сигналов с прогнозом до %d дней.", md)))
      tagList(
        card(card_header(sprintf("⚡ Быстрые (%d дн.): %d сигналов", md, nrow(tdf))),
          card_body(
            sliderInput("short_max_days", "Макс. дней в сделке", min = 1, max = 7, value = md, step = 1, post = " дн.", width = "100%"),
            DTOutput("shorttrades_table")))
      )
    }
  })

  output$signals_active <- renderUI({
    df <- signals_data(); req(df)
    if (isTRUE(input$signals_coint_only)) df <- df[df$is_coint == TRUE, ]
    min_corr <- if (isTruthy(input$signals_min_corr)) input$signals_min_corr else 70
    df <- df[df$corr >= min_corr, , drop = FALSE]
    active <- df[df$signal_type != "wait", ]
    active <- active[order(-abs(active$z_now)), ]

    if (nrow(active) == 0) {
      return(div(style = "text-align:center;padding:30px;color:#8b949e;",
        tags$i(class = "fas fa-check-circle fa-2x",
               style = "display:block;margin-bottom:10px;color:#3fb950;"),
        p("Нет активных сигналов. Все пары в нейтральной зоне.")))
    }

    # Calculator inputs (sigcalc_ prefix for this tab)
    ci <- get_calc_inputs(input, "sigcalc_")
    pw <- price_wide()

    top <- head(active, 12)
    rows <- lapply(seq_len(nrow(top)), function(i) {
      r <- top[i, ]
      is_short <- isTRUE(r$signal_type == "short_a")
      sig_col  <- if (is_short) RED else GREEN
      sig_icon <- if (is_short) "📉" else "📈"
      str_col  <- switch(r$strength,
        "Сильный"     = GREEN,
        "Прогнозный"  = ORANGE,
        "Формируется" = BLUE,
        GRAY)

      # Build signal list for calc_signal_pnl
      hr <- if (!is.na(r$hedge_ratio)) r$hedge_ratio else 1
      bt <- if (!is.null(pw)) pair_backtest_stats(pw, r$A, r$B, hr) else NULL
      s <- list(
        A = r$A, B = r$B, signal_type = r$signal_type, z_now = r$z_now,
        halflife = r$halflife, bt = bt, strength = r$strength
      )
      v <- calc_signal_pnl(s, ci$cap, ci$lev, ci$taker, ci$funding)
      pair_id <- paste0(gsub("/", ".", r$A), "_", gsub("/", ".", r$B))

      tags$div(style = paste0(
        "border:1px solid ", sig_col, ";border-radius:10px;padding:14px 16px;",
        "margin-bottom:10px;background:", BG, ";"),
        # Star + header row
        div(style = "display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;",
          tags$span(style = paste0("font-size:1.05rem;font-weight:700;color:", sig_col, ";"),
            sig_icon, " ", r$signal),
          tags$button(
            class = "btn btn-link btn-sm",
            style = "color:#f7931a;font-size:1.2rem;padding:0;border:none;background:none;cursor:pointer;",
            onclick = sprintf("Shiny.setInputValue('fav_toggle', '%s', {priority:'event'})", pair_id),
            title = "Добавить в избранное",
            tags$i(id = paste0("fav_icon_", pair_id), class = "far fa-star"))
        ),
        layout_columns(col_widths = c(6, 3, 3),
          div(
            tags$span(style = paste0("font-size:1.05rem;font-weight:700;color:", sig_col, ";"),
              sig_icon, " ", r$signal),
            tags$br(),
            tags$span(style = "font-size:0.82rem;color:#8b949e;",
              paste0("Корр: ", r$corr, "% | ",
                     if (r$is_coint) "✓ Коинтегр." else "Нет коинтегр.",
                     if (!is.na(r$halflife)) paste0(" | HL: ", r$halflife, "д") else ""))
          ),
          div(style = "text-align:center;",
            tags$div(style = "font-size:0.75rem;color:#8b949e;", "Z сейчас"),
            tags$div(style = paste0("font-size:1.4rem;font-weight:800;color:",
                                    if (abs(r$z_now) >= 2) RED else ORANGE, ";"),
              r$z_now)
          ),
          div(style = "text-align:center;",
            tags$div(style = "font-size:0.75rem;color:#8b949e;", "Z завтра"),
            tags$div(style = paste0("font-size:1.4rem;font-weight:800;color:",
                                    if (abs(r$z_forecast) >= 2) RED else ORANGE, ";"),
              r$z_forecast),
            badge(r$strength, str_col)
          )
        ),
        calc_block_ui(v)
      )
    })

    tagList(
      tags$h6(style = "color:#e6edf3;margin-bottom:12px;",
        paste0("🔔 Активные сигналы: ", nrow(active), " пар")),
      tagList(rows),
      if (nrow(active) > 12)
        p(style = "color:#8b949e;font-size:0.82rem;",
          paste0("Показаны топ-12 из ", nrow(active), ". Полный список в таблице ниже."))
    )
  })

  output$signals_table <- renderDT({
    df <- signals_data(); req(df)
    if (isTRUE(input$signals_coint_only)) df <- df[df$is_coint == TRUE, ]
    min_corr <- if (isTruthy(input$signals_min_corr)) input$signals_min_corr else 70
    df <- df[df$corr >= min_corr, , drop = FALSE]
    out <- data.frame(
      "Пара"        = paste0(df$A, " / ", df$B),
      "Z сейчас"    = df$z_now,
      "Z прогноз"   = df$z_forecast,
      "Сигнал"      = df$signal,
      "Сила"        = df$strength,
      "Коинтегр."   = ifelse(df$is_coint, "✅", "—"),
      "Корреляция"  = paste0(df$corr, "%"),
      stringsAsFactors = FALSE, check.names = FALSE
    )
    datatable(out, rownames = FALSE,
              options = list(pageLength = 25, dom = "tip", scrollX = TRUE,
                             order = list(list(1, "desc"))),
              style = "bootstrap5", class = "table-dark table-sm")
  })

  # ══════════════════════════════════════════════════════════════════════════
  # ТАБ: Избранное — трекинг сигналов
  # ══════════════════════════════════════════════════════════════════════════
  observeEvent(input$fav_toggle, {
    pair_id <- input$fav_toggle
    if (is.null(pair_id) || nchar(pair_id) < 3) return()

    # Parse pair_id: "BTC.USD_ETH.USD" -> ta="BTC/USD", tb="ETH/USD"
    parts <- strsplit(pair_id, "_")[[1]]
    ta <- gsub("\\.", "/", parts[1])
    tb <- gsub("\\.", "/", parts[2])

    con <- dbConnect(SQLite(), DB_PATH)
    on.exit(dbDisconnect(con))

    # Check if already in favorites
    uid <- if (is.null(AUTH$user_id)) "local" else AUTH$user_id
    existing <- dbGetQuery(con,
      "SELECT id, status FROM favorites WHERE ticker_a = ? AND ticker_b = ? AND status = 'active' AND user_id = ?",
      params = list(ta, tb, uid))

    if (nrow(existing) > 0) {
      # Remove from favorites
      dbExecute(con, "DELETE FROM favorites WHERE id = ?", params = list(existing$id[1]))
      showNotification(paste0("Убрано из избранного: ", ta, " / ", tb), type = "message", duration = 3)
      return()
    }

    # Add to favorites: get current prices + signal info
    pc <- pairs_coint()
    pw <- price_wide()
    if (is.null(pc) || is.null(pw)) return()

    # Find the signal
    ta_dot <- gsub("/", ".", ta); tb_dot <- gsub("/", ".", tb)
    sig <- pc[pc$A == ta & pc$B == tb, ]
    if (nrow(sig) == 0) {
      # Try reversed
      sig <- pc[pc$A == tb & pc$B == ta, ]
    }
    if (nrow(sig) == 0) {
      showNotification("Сигнал не найден в данных", type = "error")
      return()
    }
    sig <- sig[1, ]

    # Get last prices
    price_a <- if (ta_dot %in% colnames(pw)) tail(na.omit(as.numeric(pw[[ta_dot]])), 1) else NA
    price_b <- if (tb_dot %in% colnames(pw)) tail(na.omit(as.numeric(pw[[tb_dot]])), 1) else NA

    dbExecute(con, "
      INSERT INTO favorites (pair, ticker_a, ticker_b, signal, signal_type,
        z_at_entry, price_a_entry, price_b_entry, entry_time, status, halflife, corr, user_id)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), 'active', ?, ?, ?)",
      params = list(
        paste0(ta, " / ", tb), ta, tb, sig$signal, sig$signal_type,
        sig$z_now, price_a, price_b,
        sig$halflife, round(abs(sig$corr) * 100), uid)
    )

    showNotification(paste0("Добавлено в избранное: ", ta, " / ", tb), type = "message", duration = 3)
  })

  # ── Favorites data (with live P&L) ────────────────────────────────────────
  favorites_data <- reactive({
    if (!file.exists(DB_PATH)) return(NULL)
    con <- dbConnect(SQLite(), DB_PATH)
    on.exit(dbDisconnect(con))
    tables <- dbGetQuery(con, "SELECT name FROM sqlite_master WHERE type='table' AND name='favorites'")$name
    if (!"favorites" %in% tables) return(NULL)

    uid <- if (is.null(AUTH$user_id)) "local" else AUTH$user_id
    favs <- dbGetQuery(con, "SELECT * FROM favorites WHERE user_id = ? ORDER BY created_at DESC",
                       params = list(uid))
    if (nrow(favs) == 0) return(NULL)

    # Compute live P&L for active entries
    pw <- tryCatch(price_wide(), error = function(e) NULL)
    if (!is.null(pw)) {
      for (i in seq_len(nrow(favs))) {
        if (favs$status[i] != "active") next
        ta_dot <- gsub("/", ".", favs$ticker_a[i])
        tb_dot <- gsub("/", ".", favs$ticker_b[i])
        pa_now <- if (ta_dot %in% colnames(pw)) tail(na.omit(as.numeric(pw[[ta_dot]])), 1) else NA
        pb_now <- if (tb_dot %in% colnames(pw)) tail(na.omit(as.numeric(pw[[tb_dot]])), 1) else NA
        if (is.na(pa_now) || is.na(pb_now)) next

        is_long_a <- favs$signal_type[i] == "long_a"
        pnl_a <- if (is_long_a) (pa_now / favs$price_a_entry[i] - 1) * 100
                 else (favs$price_a_entry[i] / pa_now - 1) * 100
        pnl_b <- if (!is_long_a) (pb_now / favs$price_b_entry[i] - 1) * 100
                 else (favs$price_b_entry[i] / pb_now - 1) * 100
        favs$pnl_a[i] <- round(pnl_a, 2)
        favs$pnl_b[i] <- round(pnl_b, 2)
        favs$pnl_total[i] <- round((pnl_a + pnl_b) / 2, 2)
        favs$price_a_now[i] <- pa_now
        favs$price_b_now[i] <- pb_now

        # Check if HL expired
        if (!is.na(favs$halflife[i]) && favs$halflife[i] > 0) {
          entry_t <- as.POSIXct(favs$entry_time[i])
          if (!is.na(entry_t)) {
            days_held <- as.numeric(difftime(Sys.time(), entry_t, units = "days"))
            favs$days_held[i] <- round(days_held, 1)
            favs$hl_remaining[i] <- max(0, favs$halflife[i] - days_held)
          }
        }
      }
    }
    favs
  })

  output$favorites_ui <- renderUI({
    df <- favorites_data()
    if (is.null(df) || nrow(df) == 0)
      return(placeholder_msg("Избранное пусто. Нажимайте ⭐ на карточках сигналов, чтобы добавить."))

    active <- df[df$status == "active", ]
    closed <- df[df$status != "active", ]

    fmt <- function(x) format(x, big.mark = " ", scientific = FALSE, trim = TRUE)

    # Active favorites cards
    active_cards <- if (nrow(active) > 0) {
      lapply(seq_len(nrow(active)), function(i) {
        r <- active[i, ]
        is_long <- r$signal_type == "long_a"
        sig_col <- if (is_long) GREEN else RED
        sig_icon <- if (is_long) "📈" else "📉"
        pnl_col <- if (!is.na(r$pnl_total) && r$pnl_total > 0) GREEN else if (!is.na(r$pnl_total)) RED else GRAY

        tags$div(style = paste0("border:1px solid ", sig_col, ";border-radius:14px;padding:14px 16px;",
          "margin-bottom:10px;background:", CARD, ";"),
          layout_columns(col_widths = c(5, 3, 4),
            div(
              tags$span(style = paste0("font-size:1rem;font-weight:700;color:", sig_col, ";"),
                sig_icon, " ", r$signal),
              tags$br(),
              tags$span(style = "font-size:0.82rem;color:#8b949e;",
                paste0(r$ticker_a, " / ", r$ticker_b, " · корр: ", r$corr, "%",
                       if (!is.na(r$halflife)) paste0(" · HL: ", r$halflife, "д") else "")),
              tags$br(),
              tags$span(style = "font-size:0.78rem;color:#555c6b;",
                paste0("Вход: $", r$price_a_entry, " / $", r$price_b_entry))
            ),
            div(style = "text-align:center;",
              tags$div(style = "font-size:0.72rem;color:#8b949e;", "Живой P&L"),
              tags$div(style = paste0("font-size:1.4rem;font-weight:700;color:", pnl_col, ";"),
                if (!is.na(r$pnl_total)) paste0(if (r$pnl_total > 0) "+" else "", r$pnl_total, "%") else "—"),
              if (!is.na(r$pnl_a)) tags$div(style = "font-size:0.68rem;color:#555c6b;",
                paste0(r$ticker_a, ": ", if (r$pnl_a > 0) "+" else "", r$pnl_a, "%"))
            ),
            div(style = "text-align:right;",
              tags$div(style = "font-size:0.72rem;color:#8b949e;",
                paste0("Сейчас: $", r$price_a_now, " / $", r$price_b_now)),
              if (!is.na(r$days_held))
                tags$div(style = paste0("font-size:0.78rem;font-weight:600;color:",
                  if (!is.na(r$hl_remaining) && r$hl_remaining <= 0) RED else ORANGE, ";"),
                  paste0(r$days_held, " дн. · HL: ", r$halflife, "д")),
              tags$button(
                class = "btn btn-sm btn-link",
                style = "color:#f85149;font-size:0.8rem;padding:2px 8px;",
                onclick = sprintf("Shiny.setInputValue('fav_close', '%d', {priority:'event'})", r$id),
                "✕ Закрыть")
            )
          )
        )
      })
    } else NULL

    # Closed history
    hist_cards <- NULL
    if (nrow(closed) > 0) {
      closed <- head(closed[order(-as.Date(closed$exit_time)), ], 10)
      hist_cards <- lapply(seq_len(min(5, nrow(closed))), function(i) {
        r <- closed[i, ]
        pnl_col <- if (!is.na(r$exit_pnl_pct) && r$exit_pnl_pct > 0) GREEN else RED
        tags$div(style = paste0("border:1px solid ", BORDER, ";border-radius:8px;padding:10px 14px;",
          "margin-bottom:6px;background:", CARD2, ";"),
          div(style = "display:flex;align-items:center;justify-content:space-between;",
            div(style = "font-size:0.85rem;color:#e6edf3;",
              tags$span(style = "font-weight:600;", r$pair),
              tags$span(style = "font-size:0.72rem;color:#555c6b;margin-left:10px;",
                paste0(r$entry_time, " → ", r$exit_time))),
            div(style = "text-align:right;",
              tags$span(style = "font-size:0.72rem;color:#8b949e;margin-right:10px;", r$status),
              tags$span(style = paste0("font-size:0.9rem;font-weight:700;color:", pnl_col, ";"),
                if (!is.na(r$exit_pnl_pct)) paste0(if (r$exit_pnl_pct > 0) "+" else "", r$exit_pnl_pct, "%") else "—"))
          ))
      })
    }

    tagList(
      tags$div(style = "padding:12px 18px;border-radius:10px;border:1px solid #1c2333;background:#0a0e14;margin-bottom:18px;",
        tags$span(style = "color:#8b949e;font-size:0.82rem;",
          "⭐ Избранное — трекинг реального P&L. Цены входа фиксируются при добавлении. ",
          "P&L обновляется на основе текущих рыночных цен. Закрывай позиции по TP/SL вручную.")),
      if (!is.null(active_cards)) tagList(
        tags$h6(style = "color:#e6edf3;margin-bottom:12px;",
          paste0("📊 Активные: ", nrow(active), " позиций")),
        tagList(active_cards)),
      if (!is.null(hist_cards)) tagList(
        tags$h6(style = "color:#e6edf3;margin:18px 0 12px;",
          paste0("📋 История (последние)")),
        tagList(hist_cards))
    )
  })

  # Close/delete handlers
  observeEvent(input$fav_close, {
    id <- as.integer(input$fav_close)
    pw <- price_wide()
    con <- dbConnect(SQLite(), DB_PATH)
    on.exit(dbDisconnect(con))
    fav <- dbGetQuery(con, "SELECT * FROM favorites WHERE id = ?", params = list(id))
    if (nrow(fav) == 0) return()

    # Get current prices for exit
    ta_dot <- gsub("/", ".", fav$ticker_a); tb_dot <- gsub("/", ".", fav$ticker_b)
    pe_a <- if (ta_dot %in% colnames(pw)) tail(na.omit(as.numeric(pw[[ta_dot]])), 1) else NA
    pe_b <- if (tb_dot %in% colnames(pw)) tail(na.omit(as.numeric(pw[[tb_dot]])), 1) else NA

    is_long <- fav$signal_type == "long_a"
    pnl_a <- if (is_long) (pe_a / fav$price_a_entry - 1) * 100 else (fav$price_a_entry / pe_a - 1) * 100
    pnl_b <- if (!is_long) (pe_b / fav$price_b_entry - 1) * 100 else (fav$price_b_entry / pe_b - 1) * 100
    pnl_t <- round((pnl_a + pnl_b) / 2, 2)

    dbExecute(con, "
      UPDATE favorites SET status = 'closed', exit_time = datetime('now'),
        exit_price_a = ?, exit_price_b = ?, exit_pnl_pct = ?
      WHERE id = ?",
      params = list(pe_a, pe_b, pnl_t, id))
    showNotification(paste0("Закрыто: ", fav$pair, " (", pnl_t, "%)"), type = "message")
  })

  observeEvent(input$fav_delete, {
    id <- as.integer(input$fav_delete)
    con <- dbConnect(SQLite(), DB_PATH)
    on.exit(dbDisconnect(con))
    dbExecute(con, "DELETE FROM favorites WHERE id = ?", params = list(id))
    showNotification("Удалено из истории", type = "message")
  })

  # ── ТАБ: Макс. профит (прогнозный) ──────────────────────────────────────
  # Для каждой пары с ТЕКУЩИМ сигналом (|Z|≥2) находим исторические сделки
  # с похожим entry Z → прогноз: средний профит, win rate, дата выхода
  forecast_trades <- reactive({
    df <- pairs_coint()
    pw  <- price_wide()
    if (is.null(df) || is.null(pw)) return(NULL)

    min_corr <- if (isTruthy(input$mp_min_corr)) input$mp_min_corr else 50
    min_trades <- if (isTruthy(input$mp_min_trades)) input$mp_min_trades else 3
    good <- df[!is.na(df$corr) & abs(df$corr) >= min_corr / 100, , drop = FALSE]
    if (isTRUE(input$mp_coint_only)) {
      good <- good[!is.na(good$is_coint) & good$is_coint == TRUE, , drop = FALSE]
    }
    # Only pairs with ACTIVE signal right now
    good <- good[good$signal_type != "wait", , drop = FALSE]
    if (nrow(good) == 0) return(NULL)
    good <- head(good[order(-abs(good$z_now)), ], 30)

    res <- list()
    for (i in seq_len(nrow(good))) {
      r  <- good[i, ]
      hr <- if (!is.na(r$hedge_ratio)) r$hedge_ratio else 1
      th <- pair_trades_history(pw, r$A, r$B, hr)
      if (is.null(th) || nrow(th) == 0) next
      # Find historical trades with entry_z close to current z_now (±0.5)
      z_now <- r$z_now
      similar <- th[abs(th$entry_z - z_now) <= 0.5, , drop = FALSE]
      # If not enough similar, use all trades for this pair
      if (nrow(similar) < 3) similar <- th
      if (nrow(similar) == 0) next
      # Filter by minimum trades (winrate from 1 trade is meaningless)
      if (nrow(similar) < min_trades) next

      wins <- similar[similar$pnl_pct > 0, ]
      losses <- similar[similar$pnl_pct <= 0, ]
      avg_pnl <- mean(similar$pnl_pct)
      avg_hold <- round(mean(similar$hold_days))
      win_rate <- round(nrow(wins) / nrow(similar) * 100)
      avg_win <- if (nrow(wins) > 0) mean(wins$pnl_pct) else 0
      avg_loss <- if (nrow(losses) > 0) mean(losses$pnl_pct) else 0
      best_pnl <- max(similar$pnl_pct)
      worst_pnl <- min(similar$pnl_pct)
      tp_count <- sum(similar$result == "Тейк-профит")
      sl_count <- sum(similar$result == "Стоп-лосс")

      # Expected exit date
      exit_date <- format(Sys.Date() + avg_hold, "%d.%m.%Y")

      res[[length(res) + 1]] <- data.frame(
        pair = paste0(r$A, " / ", r$B),
        ticker_a = r$A, ticker_b = r$B,
        signal = r$signal, signal_type = r$signal_type,
        z_now = z_now, z_forecast = r$z_forecast,
        direction = if (r$signal_type == "short_a")
          paste0("Шорт ", r$A, " / Лонг ", r$B) else paste0("Лонг ", r$A, " / Шорт ", r$B),
        n_hist = nrow(similar), win_rate = win_rate,
        avg_pnl = round(avg_pnl, 2), avg_hold = avg_hold,
        avg_win = round(avg_win, 2), avg_loss = round(avg_loss, 2),
        best_pnl = round(best_pnl, 2), worst_pnl = round(worst_pnl, 2),
        tp_count = tp_count, sl_count = sl_count,
        exit_date = exit_date, halflife = r$halflife,
        is_coint = r$is_coint, corr = round(abs(r$corr) * 100),
        stringsAsFactors = FALSE)
    }
    if (length(res) == 0) return(NULL)
    tdf <- do.call(rbind, res)
    # Deduplicate: normalize pair key (A/B = B/A)
    tdf$pair_key <- sapply(seq_len(nrow(tdf)), function(j) {
      x <- sort(c(tdf$ticker_a[j], tdf$ticker_b[j]))
      paste0(x[1], "/", x[2])
    })
    tdf <- tdf[order(-abs(tdf$z_now)), ]
    tdf <- tdf[!duplicated(tdf$pair_key), ]
    tdf$pair_key <- NULL
    tdf[order(-tdf$avg_pnl), ]
  })

  output$maxprofit_ui <- renderUI({
    tdf <- forecast_trades()
    if (is.null(tdf) || nrow(tdf) == 0)
      return(placeholder_msg("Нет активных сигналов прямо сейчас. Проверьте вкладку «Сигналы» — если там пусто, рынок в нейтральной зоне."))

    ci <- get_calc_inputs(input, "mpcalc_")
    pos_size <- ci$cap * ci$lev
    leg_size  <- pos_size / 2

    # Compute expected net P&L per signal
    tdf$exp_gross <- round(pos_size * tdf$avg_pnl / 100, 2)
    tdf$exp_comm  <- round(4 * leg_size * ci$taker / 100, 2)
    tdf$exp_funding <- round(pos_size * ci$funding / 100 * tdf$avg_hold * 3, 2)
    tdf$exp_net   <- round(tdf$exp_gross - tdf$exp_comm - tdf$exp_funding, 2)

    fmt <- function(x) format(x, big.mark = " ", scientific = FALSE, trim = TRUE)

    # Summary
    total_exp <- round(sum(tdf$exp_net), 2)
    avg_exp   <- round(mean(tdf$exp_net), 2)
    best_exp  <- round(max(tdf$exp_net), 2)
    n_positive <- sum(tdf$exp_net > 0)
    n_total   <- nrow(tdf)

    # Top-5 forecast cards
    top5 <- head(tdf, 5)
    cards <- lapply(seq_len(nrow(top5)), function(i) {
      r <- top5[i, ]
      pnl_col <- if (r$exp_net > 0) GREEN else RED
      wr_col  <- if (r$win_rate >= 70) GREEN else if (r$win_rate >= 50) ORANGE else RED
      sig_col <- if (r$signal_type == "short_a") RED else GREEN
      sig_icon <- if (r$signal_type == "short_a") "📉" else "📈"

      div(style = paste0("border:2px solid ", sig_col, ";border-radius:14px;padding:16px 18px;",
                         "margin-bottom:12px;background:", CARD, ";box-shadow:0 0 20px ", sig_col, "22;"),
        # Signal header
        div(style = paste0("font-size:1.05rem;font-weight:700;color:", sig_col, ";margin-bottom:12px;"),
          sig_icon, " ", r$signal),
        # Forecast grid
        layout_columns(col_widths = c(3, 3, 3, 3),
          div(style = paste0("padding:12px;border-radius:8px;background:", CARD2, ";border:1px solid ", BORDER, ";"),
            div(style = "font-size:0.72rem;color:#8b949e;", "Вход"),
            div(style = "font-size:0.95rem;font-weight:600;color:#e6edf3;", "Сейчас"),
            div(style = "font-size:0.78rem;color:#555c6b;", paste0("Z = ", r$z_now))),
          div(style = paste0("padding:12px;border-radius:8px;background:", CARD2, ";border:1px solid ", BORDER, ";"),
            div(style = "font-size:0.72rem;color:#8b949e;", "Выход (прогноз)"),
            div(style = "font-size:0.95rem;font-weight:600;color:#e6edf3;", paste0("~", r$avg_hold, " дн.")),
            div(style = "font-size:0.72rem;color:#58a6ff;", paste0("к ", r$exit_date))),
          div(style = paste0("padding:12px;border-radius:8px;background:", CARD2, ";border:1px solid ", BORDER, ";"),
            div(style = "font-size:0.72rem;color:#8b949e;", "По истории"),
            div(style = paste0("font-size:1.1rem;font-weight:700;color:", wr_col, ";"), paste0(r$win_rate, "% win")),
            div(style = "font-size:0.68rem;color:#555c6b;", paste0(r$n_hist, " сделок · ТП:", r$tp_count, " СЛ:", r$sl_count))),
          div(style = paste0("padding:12px;border-radius:8px;background:", CARD2, ";border:1px solid ", BORDER, ";"),
            div(style = "font-size:0.72rem;color:#8b949e;", "Ожид. профит"),
            div(style = paste0("font-size:1.3rem;font-weight:700;color:", pnl_col, ";"),
              paste0(if (r$exp_net > 0) "+" else "", "$", fmt(r$exp_net))),
            div(style = "font-size:0.68rem;color:#555c6b;",
              paste0(if (r$avg_pnl > 0) "+" else "", r$avg_pnl, "% · ",
                     "лучшая: +", r$best_pnl, "% · худшая: ", r$worst_pnl, "%")))
        ),
        # TP / SL block
        {
          tp_gross <- round(pos_size * r$avg_win / 100, 2)
          sl_gross <- round(pos_size * abs(r$avg_loss) / 100, 2)
          tp_net <- round(tp_gross - r$exp_comm - r$exp_funding, 2)
          sl_net <- round(-(sl_gross + r$exp_comm + r$exp_funding), 2)
          z_abs <- abs(r$z_now)
          tp_distance <- round(z_abs - 0.5, 2)
          sl_distance <- round(3.5 - z_abs, 2)
          div(style = "margin-top:10px;",
            layout_columns(col_widths = c(6, 6),
              div(style = paste0("text-align:center;padding:10px;border-radius:8px;",
                                 "background:#0f2a1a;border:1px solid ", GREEN, ";"),
                div(style = "font-size:0.72rem;color:#8b949e;", "🎯 Тейк-профит (TP)"),
                div(style = paste0("font-size:1.2rem;font-weight:700;color:", GREEN, ";"),
                  paste0("+$", fmt(tp_net))),
                div(style = "font-size:0.72rem;color:#56d364;font-weight:500;",
                  paste0("+", r$avg_win, "% · Z сейчас ", sprintf("%+.2f", r$z_now), " → ±0.5 · ", tp_distance, "σ до цели"))),
              div(style = paste0("text-align:center;padding:10px;border-radius:8px;",
                                 "background:#2a0f0f;border:1px solid ", RED, ";"),
                div(style = "font-size:0.72rem;color:#8b949e;", "🛑 Стоп-лосс (SL)"),
                div(style = paste0("font-size:1.2rem;font-weight:700;color:", RED, ";"),
                  paste0("$", fmt(sl_net))),
                div(style = "font-size:0.72rem;color:#f85149;font-weight:500;",
                  paste0("-", abs(r$avg_loss), "% · Z сейчас ", sprintf("%+.2f", r$z_now), " → ±3.5 · ", sl_distance, "σ до стопа")))
            ))
        },
        # Per-leg price levels (for MEXC: where to set TP/SL for each coin)
        {
          pw <- price_wide()
          ta_norm <- if (r$ticker_a %in% colnames(pw)) r$ticker_a else gsub("/", ".", r$ticker_a)
          tb_norm <- if (r$ticker_b %in% colnames(pw)) r$ticker_b else gsub("/", ".", r$ticker_b)
          price_a <- if (!is.null(pw) && ta_norm %in% colnames(pw)) tail(na.omit(as.numeric(pw[[ta_norm]])), 1) else NA
          price_b <- if (!is.null(pw) && tb_norm %in% colnames(pw)) tail(na.omit(as.numeric(pw[[tb_norm]])), 1) else NA
          is_long_a <- r$signal_type == "long_a"
          # Per-leg price targets for MEXC: Long → TP up, SL down. Short → TP down, SL up.
          if (!is.na(price_a) && !is.na(price_b)) {
            rnd <- if (price_a >= 10) 2 else if (price_a >= 1) 3 else if (price_a >= 0.01) 4 else 6
            rnd_b <- if (price_b >= 10) 2 else if (price_b >= 1) 3 else if (price_b >= 0.01) 4 else 6
            tp_a <- if (is_long_a) round(price_a * (1 + r$avg_win / 100), rnd)
                    else round(price_a * (1 - r$avg_win / 100), rnd)
            sl_a <- if (is_long_a) round(price_a * (1 + r$avg_loss / 100), rnd)
                    else round(price_a * (1 - r$avg_loss / 100), rnd)
            tp_b <- if (!is_long_a) round(price_b * (1 + r$avg_win / 100), rnd_b)
                    else round(price_b * (1 - r$avg_win / 100), rnd_b)
            sl_b <- if (!is_long_a) round(price_b * (1 + r$avg_loss / 100), rnd_b)
                    else round(price_b * (1 - r$avg_loss / 100), rnd_b)
            div(style = paste0("margin-top:10px;padding:12px 14px;border-radius:10px;background:", CARD2,
                               ";border:1px solid ", BORDER, ";"),
              div(style = "font-size:0.82rem;font-weight:600;color:#e6edf3;margin-bottom:10px;",
                "📊 Ценовые уровни для MEXC (текущая → TP / SL)"),
              div(style = "font-size:0.8rem;line-height:1.8;",
                div(style = paste0("color:", if (is_long_a) GREEN else RED, ";"),
                  if (is_long_a) paste0("📈 Лонг ", r$ticker_a, ": $", price_a, " → TP $", tp_a, " (+", r$avg_win, "%) | SL $", sl_a, " (", r$avg_loss, "%)")
                  else paste0("📉 Шорт ", r$ticker_a, ": $", price_a, " → TP $", tp_a, " (+", r$avg_win, "%) | SL $", sl_a, " (", r$avg_loss, "%)")),
                div(style = paste0("color:", if (!is_long_a) GREEN else RED, ";"),
                  if (!is_long_a) paste0("📈 Лонг ", r$ticker_b, ": $", price_b, " → TP $", tp_b, " (+", r$avg_win, "%) | SL $", sl_b, " (", r$avg_loss, "%)")
                  else paste0("📉 Шорт ", r$ticker_b, ": $", price_b, " → TP $", tp_b, " (+", r$avg_win, "%) | SL $", sl_b, " (", r$avg_loss, "%)")))
            )
          }
        },
        # Footer
        div(style = "margin-top:10px;font-size:0.78rem;color:#8b949e;",
          if (r$is_coint) "✅ Коинтегрированы" else "⚠️ Не коинтегрированы",
          "  ·  Корр: ", r$corr, "%",
          if (!is.na(r$halflife)) paste0("  ·  HL: ", r$halflife, "д") else "",
          "  ·  Z прогноз: ", r$z_forecast)
      )
    })

    tagList(
      # Summary
      div(style = paste0("padding:18px 22px;border-radius:14px;border:1px solid ", BORDER,
                         ";background:linear-gradient(135deg,", CARD, ",", CARD2, ");margin-bottom:18px;"),
        layout_columns(col_widths = c(3, 3, 3, 3),
          div(style = "text-align:center;",
            div(style = "font-size:0.75rem;color:#8b949e;", "Активных сигналов"),
            div(style = "font-size:1.5rem;font-weight:700;color:#e6edf3;", n_total),
            div(style = "font-size:0.68rem;color:#555c6b;", "с входом прямо сейчас")),
          div(style = "text-align:center;",
            div(style = "font-size:0.75rem;color:#8b949e;", "Суммарный прогноз"),
            div(style = paste0("font-size:1.5rem;font-weight:700;color:",
                               if (total_exp > 0) GREEN else RED, ";"),
              paste0(if (total_exp > 0) "+" else "", "$", fmt(total_exp))),
            div(style = "font-size:0.68rem;color:#555c6b;", "чистыми по всем")),
          div(style = "text-align:center;",
            div(style = "font-size:0.75rem;color:#8b949e;", "Средний прогноз"),
            div(style = paste0("font-size:1.5rem;font-weight:700;color:",
                               if (avg_exp > 0) GREEN else RED, ";"),
              paste0(if (avg_exp > 0) "+" else "", "$", avg_exp)),
            div(style = "font-size:0.68rem;color:#555c6b;", "на сделку чистыми")),
          div(style = "text-align:center;",
            div(style = "font-size:0.75rem;color:#8b949e;", "Прибыльных"),
            div(style = paste0("font-size:1.5rem;font-weight:700;color:",
                               if (n_positive >= n_total / 2) GREEN else ORANGE, ";"),
              paste0(n_positive, "/", n_total)),
            div(style = "font-size:0.68rem;color:#555c6b;", "по прогнозу"))
        )
      ),
      tags$div(style = "padding:12px 18px;border-radius:10px;border:1px solid #1c2333;background:#0a0e14;margin-bottom:18px;",
        tags$span(style = "color:#8b949e;font-size:0.82rem;",
          "🔮 ПРОГНОЗ: для каждой пары с активным сигналом (|Z| ≥ 2) найдены исторические сделки ",
          "с похожим Z на входе. Средний профит и win rate — прогноз на текущую сделку. ",
          "Дата выхода = сегодня + среднее время удержания.")),
      tags$h6(style = "color:#e6edf3;margin-bottom:12px;font-size:0.88rem;", "🏆 Топ-5 прогнозов"),
      tagList(cards),
      tags$h6(style = "color:#e6edf3;margin:18px 0 12px;font-size:0.88rem;", "📋 Все прогнозы"),
      DTOutput("maxprofit_table")
    )
  })

  output$maxprofit_table <- renderDT({
    tdf <- forecast_trades(); req(tdf)
    ci <- get_calc_inputs(input, "mpcalc_")
    pos_size <- ci$cap * ci$lev
    leg_size  <- pos_size / 2
    tdf$exp_gross <- round(pos_size * tdf$avg_pnl / 100, 2)
    tdf$exp_comm  <- round(4 * leg_size * ci$taker / 100, 2)
    tdf$exp_funding <- round(pos_size * ci$funding / 100 * tdf$avg_hold * 3, 2)
    tdf$exp_net   <- round(tdf$exp_gross - tdf$exp_comm - tdf$exp_funding, 2)
    tdf$tp_net <- round(pos_size * tdf$avg_win / 100 - tdf$exp_comm - tdf$exp_funding, 2)
    tdf$sl_net <- round(-(pos_size * abs(tdf$avg_loss) / 100 + tdf$exp_comm + tdf$exp_funding), 2)

    out <- data.frame(
      "Пара" = tdf$pair, "Сигнал" = tdf$signal,
      "Z сейчас" = tdf$z_now, "Win rate %" = tdf$win_rate,
      "Сделок" = tdf$n_hist, "Ср. профит %" = tdf$avg_pnl,
      "Ср. дней" = tdf$avg_hold, "Выход к" = tdf$exit_date,
      "TP %" = tdf$avg_win, "TP $" = tdf$tp_net,
      "SL %" = tdf$avg_loss, "SL $" = tdf$sl_net,
      "Прогноз $" = tdf$exp_net,
      stringsAsFactors = FALSE, check.names = FALSE)
    datatable(out, rownames = FALSE,
              options = list(pageLength = 20, dom = "tip", scrollX = TRUE,
                             order = list(list(5, "desc"))),
              style = "bootstrap5", class = "table-dark table-sm") |>
      formatStyle("Ср. профит %",
        color = styleInterval(0, c("#f85149", "#3fb950")), fontWeight = "bold") |>
      formatStyle("Прогноз $",
        color = styleInterval(0, c("#f85149", "#3fb950")), fontWeight = "bold") |>
      formatStyle("TP $",
        color = styleInterval(0, c("#f85149", "#3fb950")), fontWeight = "bold") |>
      formatStyle("SL $",
        color = styleInterval(0, c("#3fb950", "#f85149")), fontWeight = "bold") |>
      formatStyle("Win rate %",
        color = styleInterval(c(50, 70), c("#f85149", "#f7931a", "#3fb950")), fontWeight = "bold")
  })

  # ── ТАБ: Короткие сделки (прогнозные, до 7 дней) ────────────────────────
  shortforecast_data <- reactive({
    df <- pairs_coint()
    pw  <- price_wide()
    if (is.null(df) || is.null(pw)) return(NULL)

    min_corr <- if (isTruthy(input$short_min_corr)) input$short_min_corr else 50
    max_days <- if (isTruthy(input$short_max_days)) input$short_max_days else 7
    min_trades <- if (isTruthy(input$short_min_trades)) input$short_min_trades else 3
    good <- df[!is.na(df$corr) & abs(df$corr) >= min_corr / 100, , drop = FALSE]
    if (isTRUE(input$short_coint_only)) {
      good <- good[!is.na(good$is_coint) & good$is_coint == TRUE, , drop = FALSE]
    }
    good <- good[good$signal_type != "wait", , drop = FALSE]
    if (nrow(good) == 0) return(NULL)
    good <- head(good[order(-abs(good$z_now)), ], 30)

    res <- list()
    for (i in seq_len(nrow(good))) {
      r  <- good[i, ]
      hr <- if (!is.na(r$hedge_ratio)) r$hedge_ratio else 1
      th <- pair_trades_history(pw, r$A, r$B, hr)
      if (is.null(th) || nrow(th) == 0) next
      z_now <- r$z_now
      similar <- th[abs(th$entry_z - z_now) <= 0.5, , drop = FALSE]
      if (nrow(similar) < 3) similar <- th
      if (nrow(similar) == 0) next
      # Only short historical trades (hold <= max_days)
      similar <- similar[similar$hold_days <= max_days, , drop = FALSE]
      if (nrow(similar) < 1) next
      if (nrow(similar) < min_trades) next

      wins <- similar[similar$pnl_pct > 0, ]
      losses <- similar[similar$pnl_pct <= 0, ]
      avg_pnl <- mean(similar$pnl_pct)
      avg_hold <- round(mean(similar$hold_days))
      win_rate <- round(nrow(wins) / nrow(similar) * 100)
      avg_win <- if (nrow(wins) > 0) mean(wins$pnl_pct) else 0
      avg_loss <- if (nrow(losses) > 0) mean(losses$pnl_pct) else 0
      best_pnl <- max(similar$pnl_pct)
      worst_pnl <- min(similar$pnl_pct)
      tp_count <- sum(similar$result == "Тейк-профит")
      sl_count <- sum(similar$result == "Стоп-лосс")
      exit_date <- format(Sys.Date() + avg_hold, "%d.%m.%Y")

      res[[length(res) + 1]] <- data.frame(
        pair = paste0(r$A, " / ", r$B),
        ticker_a = r$A, ticker_b = r$B,
        signal = r$signal, signal_type = r$signal_type,
        z_now = z_now, z_forecast = r$z_forecast,
        direction = if (r$signal_type == "short_a")
          paste0("Шорт ", r$A, " / Лонг ", r$B) else paste0("Лонг ", r$A, " / Шорт ", r$B),
        n_hist = nrow(similar), win_rate = win_rate,
        avg_pnl = round(avg_pnl, 2), avg_hold = avg_hold,
        avg_win = round(avg_win, 2), avg_loss = round(avg_loss, 2),
        best_pnl = round(best_pnl, 2), worst_pnl = round(worst_pnl, 2),
        tp_count = tp_count, sl_count = sl_count,
        exit_date = exit_date, halflife = r$halflife,
        is_coint = r$is_coint, corr = round(abs(r$corr) * 100),
        stringsAsFactors = FALSE)
    }
    if (length(res) == 0) return(NULL)
    tdf <- do.call(rbind, res)
    # Deduplicate: normalize pair key (A/B = B/A)
    tdf$pair_key <- sapply(seq_len(nrow(tdf)), function(j) {
      x <- sort(c(tdf$ticker_a[j], tdf$ticker_b[j]))
      paste0(x[1], "/", x[2])
    })
    tdf <- tdf[order(-abs(tdf$z_now)), ]
    tdf <- tdf[!duplicated(tdf$pair_key), ]
    tdf$pair_key <- NULL
    tdf[order(-tdf$avg_pnl), ]
  })

  output$shorttrades_ui <- renderUI({
    tdf <- shortforecast_data()
    max_days <- if (isTruthy(input$short_max_days)) input$short_max_days else 7
    if (is.null(tdf) || nrow(tdf) == 0)
      return(placeholder_msg(paste0("Нет активных сигналов с прогнозом до ", max_days, " дней.")))

    ci <- get_calc_inputs(input, "shortcalc_")
    pos_size <- ci$cap * ci$lev
    leg_size  <- pos_size / 2

    tdf$exp_gross <- round(pos_size * tdf$avg_pnl / 100, 2)
    tdf$exp_comm  <- round(4 * leg_size * ci$taker / 100, 2)
    tdf$exp_funding <- round(pos_size * ci$funding / 100 * tdf$avg_hold * 3, 2)
    tdf$exp_net   <- round(tdf$exp_gross - tdf$exp_comm - tdf$exp_funding, 2)

    total_exp <- round(sum(tdf$exp_net), 2)
    avg_exp   <- round(mean(tdf$exp_net), 2)
    best_exp  <- round(max(tdf$exp_net), 2)
    avg_hold  <- round(mean(tdf$avg_hold), 1)
    n_positive <- sum(tdf$exp_net > 0)
    n_total   <- nrow(tdf)

    fmt <- function(x) format(x, big.mark = " ", scientific = FALSE, trim = TRUE)

    top5 <- head(tdf, 5)
    cards <- lapply(seq_len(nrow(top5)), function(i) {
      r <- top5[i, ]
      pnl_col <- if (r$exp_net > 0) GREEN else RED
      wr_col  <- if (r$win_rate >= 70) GREEN else if (r$win_rate >= 50) ORANGE else RED
      sig_col <- if (r$signal_type == "short_a") RED else GREEN
      sig_icon <- if (r$signal_type == "short_a") "📉" else "📈"
      is_fast <- r$avg_hold <= 3
      speed_col <- if (is_fast) ORANGE else BLUE

      div(style = paste0("border:2px solid ", sig_col, ";border-radius:14px;padding:16px 18px;",
                         "margin-bottom:12px;background:", CARD, ";box-shadow:0 0 20px ", sig_col, "22;"),
        div(style = paste0("font-size:1.05rem;font-weight:700;color:", sig_col, ";margin-bottom:12px;"),
          sig_icon, " ", r$signal),
        layout_columns(col_widths = c(3, 3, 3, 3),
          div(style = paste0("padding:12px;border-radius:8px;background:", CARD2, ";border:1px solid ", BORDER, ";"),
            div(style = "font-size:0.72rem;color:#8b949e;", "Вход"),
            div(style = "font-size:0.95rem;font-weight:600;color:#e6edf3;", "Сейчас"),
            div(style = "font-size:0.78rem;color:#555c6b;", paste0("Z = ", r$z_now))),
          div(style = paste0("padding:12px;border-radius:8px;background:", CARD2, ";border:1px solid ", BORDER, ";"),
            div(style = "font-size:0.72rem;color:#8b949e;", "Выход (прогноз)"),
            div(style = paste0("font-size:0.95rem;font-weight:600;color:", speed_col, ";"), paste0("~", r$avg_hold, " дн.")),
            div(style = "font-size:0.72rem;color:#58a6ff;", paste0("к ", r$exit_date))),
          div(style = paste0("padding:12px;border-radius:8px;background:", CARD2, ";border:1px solid ", BORDER, ";"),
            div(style = "font-size:0.72rem;color:#8b949e;", "По истории"),
            div(style = paste0("font-size:1.1rem;font-weight:700;color:", wr_col, ";"), paste0(r$win_rate, "% win")),
            div(style = "font-size:0.68rem;color:#555c6b;", paste0(r$n_hist, " коротких сделок · ТП:", r$tp_count, " СЛ:", r$sl_count))),
          div(style = paste0("padding:12px;border-radius:8px;background:", CARD2, ";border:1px solid ", BORDER, ";"),
            div(style = "font-size:0.72rem;color:#8b949e;", "Ожид. профит"),
            div(style = paste0("font-size:1.3rem;font-weight:700;color:", pnl_col, ";"),
              paste0(if (r$exp_net > 0) "+" else "", "$", fmt(r$exp_net))),
            div(style = "font-size:0.68rem;color:#555c6b;",
              paste0(if (r$avg_pnl > 0) "+" else "", r$avg_pnl, "% · лучшая: +", r$best_pnl, "%")))
        ),
        # TP / SL block
        {
          tp_gross <- round(pos_size * r$avg_win / 100, 2)
          sl_gross <- round(pos_size * abs(r$avg_loss) / 100, 2)
          tp_net <- round(tp_gross - r$exp_comm - r$exp_funding, 2)
          sl_net <- round(-(sl_gross + r$exp_comm + r$exp_funding), 2)
          z_abs <- abs(r$z_now)
          tp_distance <- round(z_abs - 0.5, 2)
          sl_distance <- round(3.5 - z_abs, 2)
          div(style = "margin-top:10px;",
            layout_columns(col_widths = c(6, 6),
              div(style = paste0("text-align:center;padding:10px;border-radius:8px;",
                                 "background:#0f2a1a;border:1px solid ", GREEN, ";"),
                div(style = "font-size:0.72rem;color:#8b949e;", "🎯 Тейк-профит (TP)"),
                div(style = paste0("font-size:1.2rem;font-weight:700;color:", GREEN, ";"),
                  paste0("+$", fmt(tp_net))),
                div(style = "font-size:0.72rem;color:#56d364;font-weight:500;",
                  paste0("+", r$avg_win, "% · Z сейчас ", sprintf("%+.2f", r$z_now), " → ±0.5 · ", tp_distance, "σ до цели"))),
              div(style = paste0("text-align:center;padding:10px;border-radius:8px;",
                                 "background:#2a0f0f;border:1px solid ", RED, ";"),
                div(style = "font-size:0.72rem;color:#8b949e;", "🛑 Стоп-лосс (SL)"),
                div(style = paste0("font-size:1.2rem;font-weight:700;color:", RED, ";"),
                  paste0("$", fmt(sl_net))),
                div(style = "font-size:0.72rem;color:#f85149;font-weight:500;",
                  paste0("-", abs(r$avg_loss), "% · Z сейчас ", sprintf("%+.2f", r$z_now), " → ±3.5 · ", sl_distance, "σ до стопа")))
            ))
        },
        # Per-leg price levels
        {
          pw <- price_wide()
          ta_norm <- if (r$ticker_a %in% colnames(pw)) r$ticker_a else gsub("/", ".", r$ticker_a)
          tb_norm <- if (r$ticker_b %in% colnames(pw)) r$ticker_b else gsub("/", ".", r$ticker_b)
          price_a <- if (!is.null(pw) && ta_norm %in% colnames(pw)) tail(na.omit(as.numeric(pw[[ta_norm]])), 1) else NA
          price_b <- if (!is.null(pw) && tb_norm %in% colnames(pw)) tail(na.omit(as.numeric(pw[[tb_norm]])), 1) else NA
          is_long_a <- r$signal_type == "long_a"
          if (!is.na(price_a) && !is.na(price_b)) {
            rnd <- if (price_a >= 10) 2 else if (price_a >= 1) 3 else if (price_a >= 0.01) 4 else 6
            rnd_b <- if (price_b >= 10) 2 else if (price_b >= 1) 3 else if (price_b >= 0.01) 4 else 6
            tp_a <- if (is_long_a) round(price_a * (1 + r$avg_win / 100), rnd)
                    else round(price_a * (1 - r$avg_win / 100), rnd)
            sl_a <- if (is_long_a) round(price_a * (1 + r$avg_loss / 100), rnd)
                    else round(price_a * (1 - r$avg_loss / 100), rnd)
            tp_b <- if (!is_long_a) round(price_b * (1 + r$avg_win / 100), rnd_b)
                    else round(price_b * (1 - r$avg_win / 100), rnd_b)
            sl_b <- if (!is_long_a) round(price_b * (1 + r$avg_loss / 100), rnd_b)
                    else round(price_b * (1 - r$avg_loss / 100), rnd_b)
            div(style = paste0("margin-top:10px;padding:12px 14px;border-radius:10px;background:", CARD2,
                               ";border:1px solid ", BORDER, ";"),
              div(style = "font-size:0.82rem;font-weight:600;color:#e6edf3;margin-bottom:10px;",
                "📊 Ценовые уровни для MEXC (текущая → TP / SL)"),
              div(style = "font-size:0.8rem;line-height:1.8;",
                div(style = paste0("color:", if (is_long_a) GREEN else RED, ";"),
                  paste0(if (is_long_a) "📈 Лонг " else "📉 Шорт ", r$ticker_a, ": $", price_a, " → TP $", tp_a, " (+", r$avg_win, "%) | SL $", sl_a, " (", r$avg_loss, "%)")),
                div(style = paste0("color:", if (!is_long_a) GREEN else RED, ";"),
                  paste0(if (!is_long_a) "📈 Лонг " else "📉 Шорт ", r$ticker_b, ": $", price_b, " → TP $", tp_b, " (+", r$avg_win, "%) | SL $", sl_b, " (", r$avg_loss, "%)")))
            )
          }
        },
        div(style = "margin-top:10px;font-size:0.78rem;color:#8b949e;",
          if (is_fast) "⚡ Быстрая сделка (≤3 дней)" else "",
          if (r$is_coint) "  ✅ Коинтегрированы" else "  ⚠️ Не коинтегрированы",
          "  ·  Корр: ", r$corr, "%")
      )
    })

    tagList(
      div(style = paste0("padding:18px 22px;border-radius:14px;border:1px solid ", BORDER,
                         ";background:linear-gradient(135deg,", CARD, ",", CARD2, ");margin-bottom:18px;"),
        layout_columns(col_widths = c(3, 3, 3, 3),
          div(style = "text-align:center;",
            div(style = "font-size:0.75rem;color:#8b949e;", paste0("Сигналов до ", max_days, " дн.")),
            div(style = "font-size:1.5rem;font-weight:700;color:#e6edf3;", n_total),
            div(style = "font-size:0.68rem;color:#555c6b;", paste0("сред. ", avg_hold, " дн."))),
          div(style = "text-align:center;",
            div(style = "font-size:0.75rem;color:#8b949e;", "Суммарный прогноз"),
            div(style = paste0("font-size:1.5rem;font-weight:700;color:",
                               if (total_exp > 0) GREEN else RED, ";"),
              paste0(if (total_exp > 0) "+" else "", "$", fmt(total_exp))),
            div(style = "font-size:0.68rem;color:#555c6b;", "чистыми")),
          div(style = "text-align:center;",
            div(style = "font-size:0.75rem;color:#8b949e;", "Лучший прогноз"),
            div(style = "font-size:1.5rem;font-weight:700;color:#3fb950;",
              paste0("+$", fmt(best_exp))),
            div(style = "font-size:0.68rem;color:#555c6b;", "чистыми")),
          div(style = "text-align:center;",
            div(style = "font-size:0.75rem;color:#8b949e;", "Прибыльных"),
            div(style = paste0("font-size:1.5rem;font-weight:700;color:",
                               if (n_positive >= n_total / 2) GREEN else ORANGE, ";"),
              paste0(n_positive, "/", n_total)),
            div(style = "font-size:0.68rem;color:#555c6b;", "по прогнозу"))
        )
      ),
      tags$div(style = "padding:12px 18px;border-radius:10px;border:1px solid #1c2333;background:#0a0e14;margin-bottom:18px;",
        tags$span(style = "color:#8b949e;font-size:0.82rem;",
          "🔮 ПРОГНОЗ: активные сигналы (|Z| ≥ 2) с предсказанным выходом до ", max_days, " дней. ",
          "По истории схожих входов: средний профит, win rate, дата выхода. ",
          "⚡ Оранжевым отмечены сделки ≤ 3 дней (очень быстрые).")),
      tags$h6(style = "color:#e6edf3;margin-bottom:12px;font-size:0.88rem;", "⚡ Топ-5 быстрых прогнозов"),
      tagList(cards),
      tags$h6(style = "color:#e6edf3;margin:18px 0 12px;font-size:0.88rem;", "📋 Все прогнозы"),
      DTOutput("shorttrades_table")
    )
  })

  output$shorttrades_table <- renderDT({
    tdf <- shortforecast_data(); req(tdf)
    ci <- get_calc_inputs(input, "shortcalc_")
    pos_size <- ci$cap * ci$lev
    leg_size  <- pos_size / 2
    tdf$exp_gross <- round(pos_size * tdf$avg_pnl / 100, 2)
    tdf$exp_comm  <- round(4 * leg_size * ci$taker / 100, 2)
    tdf$exp_funding <- round(pos_size * ci$funding / 100 * tdf$avg_hold * 3, 2)
    tdf$exp_net   <- round(tdf$exp_gross - tdf$exp_comm - tdf$exp_funding, 2)
    tdf$tp_net <- round(pos_size * tdf$avg_win / 100 - tdf$exp_comm - tdf$exp_funding, 2)
    tdf$sl_net <- round(-(pos_size * abs(tdf$avg_loss) / 100 + tdf$exp_comm + tdf$exp_funding), 2)

    out <- data.frame(
      "Пара" = tdf$pair, "Сигнал" = tdf$signal,
      "Z сейчас" = tdf$z_now, "Win rate %" = tdf$win_rate,
      "Сделок" = tdf$n_hist, "Ср. профит %" = tdf$avg_pnl,
      "Дней" = tdf$avg_hold, "Выход к" = tdf$exit_date,
      "TP %" = tdf$avg_win, "TP $" = tdf$tp_net,
      "SL %" = tdf$avg_loss, "SL $" = tdf$sl_net,
      "Прогноз $" = tdf$exp_net,
      stringsAsFactors = FALSE, check.names = FALSE)
    datatable(out, rownames = FALSE,
              options = list(pageLength = 20, dom = "tip", scrollX = TRUE,
                             order = list(list(5, "desc"))),
              style = "bootstrap5", class = "table-dark table-sm") |>
      formatStyle("Ср. профит %",
        color = styleInterval(0, c("#f85149", "#3fb950")), fontWeight = "bold") |>
      formatStyle("Прогноз $",
        color = styleInterval(0, c("#f85149", "#3fb950")), fontWeight = "bold") |>
      formatStyle("TP $",
        color = styleInterval(0, c("#f85149", "#3fb950")), fontWeight = "bold") |>
      formatStyle("SL $",
        color = styleInterval(0, c("#3fb950", "#f85149")), fontWeight = "bold") |>
      formatStyle("Win rate %",
        color = styleInterval(c(50, 70), c("#f85149", "#f7931a", "#3fb950")), fontWeight = "bold") |>
      formatStyle("Дней",
        color = styleInterval(3, c("#f7931a", "#58a6ff")))
  })

  # ══════════════════════════════════════════════════════════════════════════
  # ТАБ: Сканеры — 4 алгоритма для поиска быстрых зависимостей
  # ══════════════════════════════════════════════════════════════════════════

  # ── 3. Correlation Breakdown: rolling vs static corr ─────────────────────
  corrbreak_scan <- reactive({
    pw <- price_wide(); req(pw)
    if (ncol(pw) < 2) return(NULL)
    rw <- as.data.frame(lapply(pw, function(x) c(NA, diff(log(as.numeric(x))))))
    coins <- colnames(rw)
    cor_static <- cor(rw, use = "pairwise.complete.obs")
    roll_window <- 30

    res <- list()
    pairs <- combn(coins, 2, simplify = FALSE)
    for (p in pairs) {
      c_static <- cor_static[p[1], p[2]]
      if (is.na(c_static) || abs(c_static) < 0.5) next  # only pairs that normally correlate

      xa <- rw[[p[1]]]; xb <- rw[[p[2]]]
      ok <- !is.na(xa) & !is.na(xb)
      if (sum(ok) < 90) next
      xa <- xa[ok]; xb <- xb[ok]
      n <- length(xa)

      # Rolling 30-day correlation
      roll_cor <- sapply(seq(roll_window, n), function(i) {
        cor(xa[(i - roll_window + 1):i], xb[(i - roll_window + 1):i])
      })
      cor_now <- tail(roll_cor, 1)
      if (is.na(cor_now)) next

      diff <- cor_now - c_static
      if (abs(diff) < 0.2) next  # only show significant breakdowns

      res[[length(res) + 1]] <- data.frame(
        A = p[1], B = p[2],
        static_corr = round(c_static * 100),
        rolling_corr = round(cor_now * 100),
        change = round(diff * 100),
        signal = if (diff < 0)
          paste0("Корреляция сломалась: ", p[1], " и ", p[2], " разошлись. Жди возврат к ", round(c_static*100), "%")
          else
          paste0("Корреляция выросла: ", p[1], " и ", p[2], " синхронизировались сильнее обычного"),
        stringsAsFactors = FALSE)
    }
    if (length(res) == 0) return(NULL)
    df <- do.call(rbind, res)
    df <- df[order(-abs(df$change)), ]
    head(df, 15)
  })

  # ── 4. Momentum: сильнейшие движения за 3/7/14 дней ──────────────────────
  momentum_scan <- reactive({
    pw <- price_wide(); req(pw)
    if (ncol(pw) < 2) return(NULL)

    res <- list()
    for (sym in colnames(pw)) {
      x <- as.numeric(pw[[sym]])
      x <- x[!is.na(x)]
      if (length(x) < 15) next
      chg3  <- (tail(x, 1) / tail(x, 4)[1] - 1) * 100
      chg7  <- (tail(x, 1) / tail(x, 8)[1] - 1) * 100
      chg14 <- (tail(x, 1) / tail(x, 15)[1] - 1) * 100
      vol7  <- sd(diff(log(tail(x, 8))), na.rm = TRUE) * sqrt(7) * 100

      # Signal: momentum + volatility
      trend <- if (chg7 > 5) "Сильный рост" else if (chg7 > 1) "Рост"
               else if (chg7 < -5) "Сильное падение" else if (chg7 < -1) "Падение"
               else "Боковик"
      action <- if (chg7 > 5 && chg3 > 0) paste0("📈 Лонг ", sym, " (моментум вверх)")
                else if (chg7 < -5 && chg3 < 0) paste0("📉 Шорт ", sym, " (моментум вниз)")
                else "Ждать"

      res[[length(res) + 1]] <- data.frame(
        ticker = sym, chg3 = round(chg3, 2), chg7 = round(chg7, 2),
        chg14 = round(chg14, 2), vol7 = round(vol7, 2),
        trend = trend, signal = action,
        stringsAsFactors = FALSE)
    }
    if (length(res) == 0) return(NULL)
    df <- do.call(rbind, res)
    df <- df[order(-abs(df$chg7)), ]
    df
  })

  # ── 7. Drawdown: глубокие просадки ──────────────────────────────────────
  drawdown_scan <- reactive({
    pw <- price_wide(); req(pw)
    if (ncol(pw) < 2) return(NULL)
    lookback <- 90  # days for high watermark
    res <- list()
    for (sym in colnames(pw)) {
      x <- as.numeric(pw[[sym]])
      x <- x[!is.na(x)]
      if (length(x) < lookback) next
      recent <- tail(x, lookback)
      high <- max(recent)
      last <- tail(recent, 1)
      dd <- (last / high - 1) * 100
      if (dd > -10) next  # only show significant drawdowns (< -10%)
      days_since_high <- lookback - which.max(recent)
      # Historical recovery rate
      full_x <- x
      n <- length(full_x)
      recoveries <- c()
      for (i in seq(30, n - 30, by = 5)) {
        h <- max(full_x[max(1, i - lookback):i])
        d <- (full_x[i] / h - 1) * 100
        if (d < -10) {
          future <- full_x[(i + 1):min(i + 30, n)]
          if (length(future) > 0) {
            best_recovery <- max(future / full_x[i] - 1) * 100
            recoveries <- c(recoveries, best_recovery)
          }
        }
      }
      avg_recovery <- if (length(recoveries) > 0) round(mean(recoveries), 1) else NA
      res[[length(res) + 1]] <- data.frame(
        ticker = sym, drawdown = round(dd, 1),
        high = round(high, 4), current = round(last, 4),
        days_from_high = days_since_high,
        avg_recovery = avg_recovery,
        signal = paste0("📈 Лонг ", sym, " (просадка ", round(dd, 1), "%, исторический отскок +",
                        if (is.na(avg_recovery)) "?" else avg_recovery, "%)"),
        stringsAsFactors = FALSE)
    }
    if (length(res) == 0) return(NULL)
    df <- do.call(rbind, res)
    df[order(df$drawdown), ]
  })

  # ── Scanner UI dispatcher ────────────────────────────────────────────────
  output$scanner_ui <- renderUI({
    st <- input$scanner_type
    if (is.null(st)) return(NULL)

    if (st == "corrbreak") {
      df <- corrbreak_scan()
      if (is.null(df) || nrow(df) == 0)
        return(placeholder_msg("Нет сломанных корреляций. Все пары ведут себя как обычно."))
      ci <- get_calc_inputs(input, "scancalc_")
      cards <- lapply(seq_len(min(6, nrow(df))), function(i) {
        r <- df[i, ]
        brk_col <- if (r$change < 0) RED else GREEN
        s <- list(
          signal_type = "long_a",
          z_now = abs(r$change) / 10, halflife = 5, bt = NULL, strength = "Сканер")
        v <- calc_signal_pnl(s, ci$cap, ci$lev, ci$taker, ci$funding)
        tags$div(style = paste0(
          "border:1px solid ", brk_col, ";border-radius:14px;padding:14px 16px;",
          "margin-bottom:10px;background:", CARD, ";"),
          layout_columns(col_widths = c(4, 3, 3, 2),
            div(
              tags$span(style = "font-size:1rem;font-weight:700;color:#e6edf3;",
                r$A, " / ", r$B),
              tags$br(),
              tags$span(style = "font-size:0.78rem;color:#8b949e;", r$signal)
            ),
            div(style = "text-align:center;",
              tags$div(style = "font-size:0.72rem;color:#8b949e;", "Обычная корр."),
              tags$div(style = "font-size:1rem;font-weight:600;color:#8b949e;",
                paste0(r$static_corr, "%"))
            ),
            div(style = "text-align:center;",
              tags$div(style = "font-size:0.72rem;color:#8b949e;", "Сейчас (30д)"),
              tags$div(style = paste0("font-size:1rem;font-weight:700;color:", brk_col, ";"),
                paste0(r$rolling_corr, "%"))
            ),
            div(style = "text-align:right;",
              tags$div(style = "font-size:0.72rem;color:#8b949e;", "Изменение"),
              tags$div(style = paste0("font-size:1.1rem;font-weight:700;color:", brk_col, ";"),
                paste0(if (r$change > 0) "+" else "", r$change, "%"))
            )
          ),
          calc_block_ui(v)
        )
      })
      tagList(
        tags$div(style = "padding:12px 18px;border-radius:10px;border:1px solid #1c2333;background:#0a0e14;margin-bottom:18px;",
          tags$span(style = "color:#8b949e;font-size:0.85rem;",
            "Пары, где корреляция за 30 дней сильно отличается от обычной. ",
            "Красное = корреляция сломалась (паритет временно нарушен — ждём возврат). ",
            "Зелёное = синхронизация усилилась. Возврат к норме — 3-7 дней.")),
        tagList(cards),
        tags$h6(style = "color:#e6edf3;margin:18px 0 12px;", "📋 Все аномалии корреляции"),
        DTOutput("corrbreak_table")
      )

    } else if (st == "momentum") {
      df <- momentum_scan()
      if (is.null(df) || nrow(df) == 0)
        return(placeholder_msg("Нет данных для momentum."))
      ci <- get_calc_inputs(input, "scancalc_")
      cards <- lapply(seq_len(min(6, nrow(df))), function(i) {
        r <- df[i, ]
        mom_col <- if (r$chg7 > 5) GREEN else if (r$chg7 < -5) RED else ORANGE
        is_signal <- r$signal != "Ждать"
        s <- list(
          signal_type = if (grepl("Шорт", r$signal)) "short_a" else "long_a",
          z_now = abs(r$chg7) / 5, halflife = 7, bt = NULL, strength = "Сканер")
        v <- if (is_signal) calc_signal_pnl(s, ci$cap, ci$lev, ci$taker, ci$funding) else NULL
        tags$div(style = paste0(
          "border:1px solid ", BORDER, ";border-radius:14px;padding:14px 16px;",
          "margin-bottom:10px;background:", CARD, ";"),
          layout_columns(col_widths = c(3, 2, 2, 2, 3),
            div(
              tags$div(style = "font-size:1rem;font-weight:700;color:#e6edf3;", r$ticker),
              tags$div(style = "font-size:0.72rem;color:#555c6b;", r$trend)
            ),
            div(style = "text-align:center;",
              tags$div(style = "font-size:0.72rem;color:#8b949e;", "3 дня"),
              tags$div(style = paste0("font-size:0.95rem;font-weight:600;color:",
                                       if (r$chg3 > 0) GREEN else RED, ";"),
                paste0(if (r$chg3 > 0) "+" else "", r$chg3, "%"))
            ),
            div(style = "text-align:center;",
              tags$div(style = "font-size:0.72rem;color:#8b949e;", "7 дней"),
              tags$div(style = paste0("font-size:1.05rem;font-weight:700;color:", mom_col, ";"),
                paste0(if (r$chg7 > 0) "+" else "", r$chg7, "%"))
            ),
            div(style = "text-align:center;",
              tags$div(style = "font-size:0.72rem;color:#8b949e;", "14 дней"),
              tags$div(style = paste0("font-size:0.95rem;font-weight:600;color:",
                                       if (r$chg14 > 0) GREEN else RED, ";"),
                paste0(if (r$chg14 > 0) "+" else "", r$chg14, "%"))
            ),
            div(style = "text-align:right;",
              tags$div(style = "font-size:0.72rem;color:#8b949e;", "Волатильность"),
              tags$div(style = "font-size:0.85rem;color:#8b949e;", paste0(r$vol7, "%/нед")),
              tags$div(style = paste0("font-size:0.82rem;font-weight:600;color:",
                                       if (is_signal) BLUE else GRAY, ";"), r$signal)
            )
          ),
          if (!is.null(v)) calc_block_ui(v)
        )
      })
      tagList(
        tags$div(style = "padding:12px 18px;border-radius:10px;border:1px solid #1c2333;background:#0a0e14;margin-bottom:18px;",
          tags$span(style = "color:#8b949e;font-size:0.85rem;",
            "Сильнейшие движения за 3/7/14 дней. ",
            "Моментум > 5% за 7 дней + продолжение за 3 дня = сигнал. ",
            "Волатильность показывает риск (чем выше, тем опаснее).")),
        tagList(cards),
        tags$h6(style = "color:#e6edf3;margin:18px 0 12px;", "📋 Все инструменты по моментуму"),
        DTOutput("momentum_table")
      )

    } else if (st == "drawdown") {
      df <- drawdown_scan()
      if (is.null(df) || nrow(df) == 0)
        return(placeholder_msg("Нет значительных просадок (>10%) на этом рынке."))
      ci <- get_calc_inputs(input, "scancalc_")
      cards <- lapply(seq_len(min(6, nrow(df))), function(i) {
        r <- df[i, ]
        dd_col <- if (r$drawdown < -30) RED else if (r$drawdown < -20) ORANGE else BLUE
        s <- list(signal_type = "long_a", z_now = abs(r$drawdown) / 10,
                  halflife = 14, bt = NULL, strength = "Drawdown")
        v <- calc_signal_pnl(s, ci$cap, ci$lev, ci$taker, ci$funding)
        div(style = paste0("border:1px solid ", dd_col, ";border-radius:14px;padding:14px 16px;margin-bottom:10px;background:", CARD, ";"),
          layout_columns(col_widths = c(3, 3, 2, 4),
            div(tags$div(style="font-size:1rem;font-weight:700;color:#e6edf3;", r$ticker),
                tags$div(style="font-size:0.72rem;color:#555c6b;",
                  paste0("макс: ", r$high, " → тек: ", r$current))),
            div(style="text-align:center;",
              tags$div(style="font-size:0.72rem;color:#8b949e;", "Просадка"),
              tags$div(style=paste0("font-size:1.3rem;font-weight:700;color:", dd_col, ";"),
                paste0(r$drawdown, "%")),
              tags$div(style="font-size:0.68rem;color:#555c6b;",
                paste0(r$days_from_high, " дн. от максимума"))),
            div(style="text-align:center;",
              tags$div(style="font-size:0.72rem;color:#8b949e;", "Ист. отскок"),
              tags$div(style="font-size:1rem;font-weight:700;color:#3fb950;",
                if(is.na(r$avg_recovery)) "?" else paste0("+", r$avg_recovery, "%"))),
            div(style="text-align:right;",
              tags$div(style="font-size:0.78rem;font-weight:500;color:#58a6ff;", r$signal))
          ),
          calc_block_ui(v)
        )
      })
      tagList(
        tags$div(style="padding:12px 18px;border-radius:10px;border:1px solid #1c2333;background:#0a0e14;margin-bottom:18px;",
          tags$span(style="color:#8b949e;font-size:0.85rem;",
            "Инструменты в просадке > 10% от 90-дневного максимума. Исторический отскок — средний возврат ",
            "после подобных просадок (по 30-дневному окну). Лонг на отскок.")),
        tagList(cards),
        tags$h6(style="color:#e6edf3;margin:18px 0 12px;", "📋 Все просадки"),
        DTOutput("drawdown_table"))
    }
  })

  # ── Scanner tables ───────────────────────────────────────────────────────
  output$corrbreak_table <- renderDT({
    df <- corrbreak_scan(); req(df)
    datatable(data.frame(
      "A" = df$A, "B" = df$B,
      "Обычная корр. %" = df$static_corr, "Сейчас (30д) %" = df$rolling_corr,
      "Изменение %" = df$change, "Сигнал" = df$signal,
      stringsAsFactors = FALSE, check.names = FALSE),
      rownames = FALSE, options = list(pageLength = 15, dom = "tip", scrollX = TRUE),
      style = "bootstrap5", class = "table-dark table-sm") |>
      formatStyle("Изменение %",
        color = styleInterval(0, c("#f85149", "#3fb950")),
        fontWeight = "bold")
  })

  output$momentum_table <- renderDT({
    df <- momentum_scan(); req(df)
    datatable(data.frame(
      "Тикер" = df$ticker, "3 дня %" = df$chg3, "7 дней %" = df$chg7,
      "14 дней %" = df$chg14, "Волат. %/нед" = df$vol7,
      "Тренд" = df$trend, "Сигнал" = df$signal,
      stringsAsFactors = FALSE, check.names = FALSE),
      rownames = FALSE, options = list(pageLength = 15, dom = "tip", scrollX = TRUE),
      style = "bootstrap5", class = "table-dark table-sm") |>
      formatStyle("7 дней %",
        color = styleInterval(c(-5, 5), c("#f85149", "#f7931a", "#3fb950")),
        fontWeight = "bold")
  })

  output$drawdown_table <- renderDT({
    df <- drawdown_scan(); req(df)
    datatable(data.frame(
      "Тикер" = df$ticker, "Просадка %" = df$drawdown,
      "Максимум" = df$high, "Текущая" = df$current,
      "Дней от макс." = df$days_from_high, "Ист. отскок %" = df$avg_recovery,
      "Сигнал" = df$signal,
      stringsAsFactors = FALSE, check.names = FALSE),
      rownames = FALSE, options = list(pageLength = 15, dom = "tip", scrollX = TRUE),
      style = "bootstrap5", class = "table-dark table-sm") |>
      formatStyle("Просадка %",
        color = styleInterval(c(-30, -20), c("#f85149", "#f7931a", "#58a6ff")),
        fontWeight = "bold")
  })

  # ══════════════════════════════════════════════════════════════════════════
  # ТАБ: AI-аналитик (DeepSeek v4 Pro)
  # ══════════════════════════════════════════════════════════════════════════
  ai_response <- reactiveVal(NULL)
  ai_loading  <- reactiveVal(FALSE)

  # Collect current market data summary for AI
  collect_ai_context <- function() {
    ctx <- list()

    # Market type
    ctx$market <- input$market_type

    # Active signals
    pc <- pairs_coint()
    if (!is.null(pc) && nrow(pc) > 0) {
      active <- pc[pc$signal_type != "wait", , drop = FALSE]
      if (nrow(active) > 0) {
        active <- head(active[order(-abs(active$z_now)), ], 10)
        ctx$signals <- lapply(seq_len(nrow(active)), function(i) {
          r <- active[i, ]
          list(
            pair = paste0(r$A, " / ", r$B),
            signal = r$signal,
            z_now = r$z_now,
            z_forecast = r$z_forecast,
            corr = round(abs(r$corr) * 100),
            is_coint = r$is_coint,
            halflife = r$halflife,
            strength = r$strength
          )
        })
      }
    }

    # Top pairs by score
    if (!is.null(pc) && nrow(pc) > 0) {
      top <- head(pc[order(-pc$score), ], 5)
      ctx$top_pairs <- lapply(seq_len(nrow(top)), function(i) {
        r <- top[i, ]
        list(
          pair = paste0(r$A, " / ", r$B),
          corr = round(abs(r$corr) * 100),
          is_coint = r$is_coint,
          halflife = r$halflife,
          score = round(r$score, 2)
        )
      })
    }

    ctx
  }

  # Build prompt for DeepSeek
  build_ai_prompt <- function(ctx) {
    market_name <- switch(ctx$market, crypto = "Криптовалюты", stocks = "Акции/ETF", forex = "Форекс")

    prompt <- paste0(
      "Ты профессиональный трейдер-аналитик. Проанализируй текущие данные рынка ",
      market_name, " и дай КОНКРЕТНЫЕ рекомендации.\n\n")

    prompt <- paste0(prompt, "## Текущие данные:\n\n")

    # Signals
    if (!is.null(ctx$signals) && length(ctx$signals) > 0) {
      prompt <- paste0(prompt, "### Активные сигналы (pairs trading, |Z| >= 2):\n")
      for (s in ctx$signals) {
        prompt <- paste0(prompt,
          sprintf("- %s: %s | Z=%.2f, прогноз Z=%.2f | корр=%d%% | %s | HL=%sд | сила=%s\n",
            s$pair, s$signal, s$z_now, s$z_forecast, s$corr,
            if (s$is_coint) "коинтегрированы" else "не коинтегр.",
            if (is.na(s$halflife)) "?" else s$halflife,
            s$strength))
      }
      prompt <- paste0(prompt, "\n")
    } else {
      prompt <- paste0(prompt, "### Активные сигналы: нет (рынок в нейтральной зоне)\n\n")
    }

    # Volatility
    if (!is.null(ctx$volatility)) {
      prompt <- paste0(prompt, sprintf(
        "### Волатильность: %s (текущая %.1f%%, отношение к истории %.2f)\n\n",
        ctx$volatility$regime, ctx$volatility$avg_vol, ctx$volatility$ratio))
    }

    # Top pairs
    if (!is.null(ctx$top_pairs) && length(ctx$top_pairs) > 0) {
      prompt <- paste0(prompt, "### Топ пар по рейтингу:\n")
      for (p in ctx$top_pairs) {
        prompt <- paste0(prompt,
          sprintf("- %s: корр=%d%% | %s | HL=%sд | score=%.2f\n",
            p$pair, p$corr, if (p$is_coint) "коинтегр." else "не коинтегр.",
            if (is.na(p$halflife)) "?" else p$halflife, p$score))
      }
      prompt <- paste0(prompt, "\n")
    }

    # Lead-lag
    if (!is.null(ctx$leadlag) && length(ctx$leadlag) > 0) {
      prompt <- paste0(prompt, "### Lead-Lag (опережения):\n")
      for (l in ctx$leadlag) {
        prompt <- paste0(prompt,
          sprintf("- %s → %s (лаг %dд): %s\n", l$leader, l$follower, l$lag, l$signal))
      }
      prompt <- paste0(prompt, "\n")
    }

    # Mean reversion
    if (!is.null(ctx$mean_reversion) && length(ctx$mean_reversion) > 0) {
      prompt <- paste0(prompt, "### Mean Reversion (отклонения от среднего):\n")
      for (m in ctx$mean_reversion) {
        prompt <- paste0(prompt,
          sprintf("- %s: Z=%.2f, %s\n", m$ticker, m$z_score, m$signal))
      }
      prompt <- paste0(prompt, "\n")
    }

    # Instructions
    prompt <- paste0(prompt,
      "## Что нужно ответить (на русском, кратко и конкретно):\n",
      "1. **Лучшая сделка прямо сейчас** — что лонгить/шортить, почему\n",
      "2. **Когда входить** — прямо сейчас или ждать\n",
      "3. **Когда выходить** — условия выхода (Z, дни)\n",
      "4. **Размер позиции** — какой % капитала, учитывая волатильность\n",
      "5. **Риски** — что может пойти не так\n",
      "6. **Альтернатива** — если лучший сигнал не подходит\n\n",
      "Формат: markdown, максимум 500 слов. Без воды.")

    prompt
  }

  # Call DeepSeek API
  call_deepseek <- function(prompt) {
    api_key <- Sys.getenv("DEEPSEEK_API_KEY", "")
    if (nchar(api_key) < 10) {
      return(list(error = "DEEPSEEK_API_KEY не задан. Добавьте ключ в Railway env vars."))
    }

    tryCatch({
      resp <- httr::POST(
        "https://api.deepseek.com/chat/completions",
        httr::add_headers(
          "Content-Type" = "application/json",
          "Authorization" = paste("Bearer", api_key)
        ),
        body = list(
          model = "deepseek-v4-pro",
          messages = list(
            list(role = "system",
                 content = "Ты профессиональный крипто-трейдер и аналитик. Отвечай на русском, конкретно, без воды. Используй markdown."),
            list(role = "user", content = prompt)
          ),
          stream = FALSE,
          max_tokens = 1500,
          temperature = 0.3
        ),
        encode = "json",
        httr::timeout(120)
      )

      if (httr::status_code(resp) != 200) {
        err <- httr::content(resp, "text", encoding = "UTF-8")
        return(list(error = paste("API error", httr::status_code(resp), ":", err)))
      }

      result <- httr::content(resp, "parsed")
      content <- result$choices[[1]]$message$content
      list(text = content, usage = result$usage)
    }, error = function(e) {
      list(error = paste("Ошибка запроса:", e$message))
    })
  }

  # Button handler
  observeEvent(input$ai_analyze, {
    ai_loading(TRUE)
    ai_response(NULL)

    ctx <- collect_ai_context()
    prompt <- build_ai_prompt(ctx)

    result <- call_deepseek(prompt)
    ai_response(result)
    ai_loading(FALSE)
  })

  # AI result UI
  output$ai_result_ui <- renderUI({
    if (ai_loading()) {
      return(div(style = "text-align:center;padding:60px 20px;",
        tags$div(style = "
          width:60px;height:60px;margin:0 auto 20px;
          border:3px solid #1c2333;border-top-color:#58a6ff;
          border-radius:50%;animation:spin 1s linear infinite;"),
        p(style = "font-size:1rem;color:#8b949e;", "AI анализирует данные..."),
        p(style = "font-size:0.78rem;color:#555c6b;", "DeepSeek v4 Pro обрабатывает сигналы и сканеры")))
    }

    res <- ai_response()
    if (is.null(res)) return(NULL)

    if (!is.null(res$error)) {
      return(div(style = paste0("padding:24px;border-radius:14px;border:1px solid ", RED,
                                ";background:#2a0f0f;text-align:center;"),
        tags$i(class = "fas fa-exclamation-triangle fa-2x",
               style = paste0("color:", RED, ";margin-bottom:12px;")),
        p(style = "font-size:0.95rem;color:#f85149;", res$error)))
    }

    # Success — render markdown
    text <- res$text
    usage <- res$usage

    tagList(
      div(style = paste0("padding:24px;border-radius:14px;border:1px solid ", BORDER,
                         ";background:", CARD, ";margin-bottom:16px;"),
        div(style = "font-size:0.85rem;font-weight:600;color:#58a6ff;margin-bottom:14px;",
          "🤖 Анализ DeepSeek v4 Pro"),
        # Render as preformatted (R Shiny doesn't have markdown renderer built-in)
        div(style = "font-size:0.9rem;color:#e6edf3;line-height:1.7;white-space:pre-wrap;word-wrap:break-word;",
          text)),
      if (!is.null(usage))
        div(style = "font-size:0.72rem;color:#555c6b;text-align:right;",
          paste0("Токены: prompt=", usage$prompt_tokens,
                 ", completion=", usage$completion_tokens,
                 ", total=", usage$total_tokens))
    )
  })

}

shinyApp(ui, server)
