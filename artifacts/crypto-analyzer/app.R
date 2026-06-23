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
  fmt <- function(x) format(x, big.mark = " ", scientific = FALSE, trim = TRUE)
  rr_col <- if (!is.na(v$rr_ratio) && v$rr_ratio >= 1.5) GREEN else ORANGE
  div(style = paste0("margin-top:14px;padding:14px 16px;border-radius:10px;",
                     "background:", CARD, ";border:1px solid ", BORDER, ";"),
    div(style = "font-size:0.85rem;font-weight:600;color:#e6edf3;margin-bottom:10px;",
      "Калькулятор прибыли (MEXC Perpetual)"),
    layout_columns(col_widths = c(3, 3, 3, 3),
      div(
        div(style = "font-size:0.72rem;color:#8b949e;", "Размер позиции"),
        div(style = "font-size:0.95rem;font-weight:600;color:#e6edf3;",
          paste0("$", fmt(v$pos_size))),
        div(style = "font-size:0.68rem;color:#555;", paste0("капитал × плечо"))
      ),
      div(
        div(style = "font-size:0.72rem;color:#8b949e;", "Комиссии (вход+выход)"),
        div(style = "font-size:0.95rem;font-weight:600;color:#f85149;",
          paste0("-$", v$comm)),
        div(style = "font-size:0.68rem;color:#555;", "4 заполнения × taker%")
      ),
      div(
        div(style = "font-size:0.72rem;color:#8b949e;", paste0("Финансирование (", v$fund_periods, " раз)")),
        div(style = "font-size:0.95rem;font-weight:600;color:#f85149;",
          paste0("-$", v$funding)),
        div(style = "font-size:0.68rem;color:#555;", paste0("за ", v$hold_days, " дн."))
      ),
      div(
        div(style = "font-size:0.72rem;color:#8b949e;", "Risk / Reward"),
        div(style = paste0("font-size:1.1rem;font-weight:700;color:", rr_col, ";"),
          if (!is.na(v$rr_ratio)) paste0("1:", round(v$rr_ratio, 1)) else "—"),
        div(style = "font-size:0.68rem;color:#555;", "профит / убыток")
      )
    ),
    div(style = "border-top:1px solid #30363d;margin:10px 0;"),
    layout_columns(col_widths = c(6, 6),
      div(style = paste0("text-align:center;padding:10px;border-radius:8px;",
                         "background:#0f2a1a;border:1px solid ", GREEN, ";"),
        div(style = "font-size:0.75rem;color:#8b949e;", "Чистая прибыль (TP)"),
        div(style = paste0("font-size:1.3rem;font-weight:700;color:", v$tp_col, ";"),
          paste0(if (v$net_tp > 0) "+" else "", "$", v$net_tp)),
        div(style = "font-size:0.68rem;color:#555;",
          paste0("+", v$gross_tp, " − ", v$comm, " − ", v$funding))
      ),
      div(style = paste0("text-align:center;padding:10px;border-radius:8px;",
                         "background:#2a0f0f;border:1px solid ", RED, ";"),
        div(style = "font-size:0.75rem;color:#8b949e;", "Чистый убыток (SL)"),
        div(style = "font-size:1.3rem;font-weight:700;color:#f85149;",
          paste0("$", v$net_sl)),
        div(style = "font-size:0.68rem;color:#555;",
          paste0("-(", v$gross_sl, " + ", v$comm, " + ", v$funding, ")"))
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

  # ── TAB 1: Данные ───────────────────────────────────────────────────────
  nav_panel("📂 Данные",
    layout_columns(col_widths = c(4, 8),
      card(
        card_header("Источник данных"),
        card_body(
          # Market type switcher
          radioButtons("market_type", NULL,
            choices = c("Crypto" = "crypto", "Акции/ETF" = "stocks", "Forex" = "forex"),
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
    uiOutput("signals_ui")
  ),

  # ── TAB 4: Понятные сигналы ─────────────────────────────────────────────
  nav_panel("💡 Понятные сигналы",
    div(style = "padding:16px 20px;border-radius:12px;border:1px solid #30363d;background:#161b22;margin-bottom:18px;",
      div(style = "font-size:0.9rem;font-weight:600;color:#e6edf3;margin-bottom:12px;",
        "Настройки калькулятора (применяются ко всем сигналам)"),
      calc_settings_ui("calc_"),
      div(style = "font-size:0.72rem;color:#555;margin-top:8px;",
        "MEXC Perpetual: taker 0.02%, maker 0.00%, финансирование ~0.01% / 8ч. ",
        "Комиссии: 4 заполнения (2 ноги × вход + выход). Измените под свой аккаунт.")
    ),
    uiOutput("clear_signals_ui")
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
      layout_columns(col_widths = c(6, 6),
        checkboxInput("mp_coint_only", "Только коинтегрированные пары", value = FALSE),
        sliderInput("mp_min_corr", "Мин. корреляция", min = 50, max = 100, value = 50, step = 5, post = "%", width = "100%")
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
      layout_columns(col_widths = c(4, 4, 4),
        checkboxInput("short_coint_only", "Только коинтегрированные", value = FALSE),
        sliderInput("short_min_corr", "Мин. корреляция", min = 50, max = 100, value = 50, step = 5, post = "%", width = "100%"),
        sliderInput("short_max_days", "Макс. дней в сделке", min = 1, max = 7, value = 7, step = 1, post = " дн.", width = "100%")
      )
    ),
    uiOutput("shorttrades_ui")
  ),

  # ── TAB 7: Сканеры ──────────────────────────────────────────────────────
  nav_panel("🔍 Сканеры",
    div(style = "padding:16px 20px;border-radius:12px;border:1px solid #1c2333;background:#0f1419;margin-bottom:18px;",
      radioButtons("scanner_type", NULL,
        choices = c("⚡ Lead-Lag" = "leadlag", "🎯 Mean Reversion" = "meanrev",
                    "🔗 Corr Breakdown" = "corrbreak", "🚀 Momentum" = "momentum",
                    "🚨 Аномалии" = "anomaly", "📦 Volume" = "volume",
                    "📉 Drawdown" = "drawdown", "🎯 Multi-TF" = "multitf",
                    "🌊 Volatility" = "volatility"),
        selected = "leadlag", inline = TRUE)
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

  # ── TAB 8: Паттерны ─────────────────────────────────────────────────────
  nav_panel("📅 Паттерны",
    uiOutput("patterns_ui")
  ),

  # ── TAB 9: Риск ─────────────────────────────────────────────────────────
  nav_panel("🛡️ Риск",
    uiOutput("risk_ui")
  ),

  # ── TAB 10: Топ-6 монет ─────────────────────────────────────────────────
  nav_panel("⭐ Топ-6 монет",
    uiOutput("top6_ui")
  )
)

# ── Server ───────────────────────────────────────────────────────────────────
server <- function(input, output, session) {

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
    div(style = "padding:14px 16px;border-radius:10px;border:1px solid #30363d;background:#0d1117;",
      tags$span(style = "color:#3fb950;font-weight:600;", "✓ БД подключена"),
      tags$br(),
      tags$span(style = "color:#adbac7;font-size:0.88rem;",
        paste0(stats$n_tickers, " тикеров · ",
               format(stats$n_rows, big.mark = " "), " записей · ",
               stats$min_d, " — ", stats$max_d)),
      tags$br(),
      tags$code(style = "color:#555;font-size:0.75rem;", DB_PATH)
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

      tags$div(style = paste0(
        "border:1px solid ", sig_col, ";border-radius:10px;padding:14px 16px;",
        "margin-bottom:10px;background:", BG, ";"),
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

  # ── ТАБ: Понятные сигналы ───────────────────────────────────────────────
  clear_signals_data <- reactive({
    df <- pairs_coint()
    pw  <- price_wide()
    if (is.null(df) || is.null(pw)) return(NULL)
    active <- df[df$signal_type != "wait", , drop = FALSE]
    if (nrow(active) == 0) return(NULL)
    active <- active[order(-abs(active$z_now)), ]
    active <- head(active, 10)

    lapply(seq_len(nrow(active)), function(i) {
      r  <- active[i, ]
      hr <- if (!is.na(r$hedge_ratio)) r$hedge_ratio else 1
      bt <- pair_backtest_stats(pw, r$A, r$B, hr)
      list(
        A = r$A, B = r$B,
        signal = r$signal, signal_type = r$signal_type,
        z_now = r$z_now, z_forecast = r$z_forecast,
        strength = r$strength, is_coint = r$is_coint,
        corr = round(abs(r$corr) * 100),
        halflife = r$halflife, hedge_ratio = hr,
        bt = bt
      )
    })
  })

  output$clear_signals_ui <- renderUI({
    items <- clear_signals_data()
    if (is.null(items)) return(placeholder_msg("Нет активных сигналов. Все пары в нейтральной зоне."))

    ci <- get_calc_inputs(input, "calc_")

    cards <- lapply(items, function(s) {
      is_short <- isTRUE(s$signal_type == "short_a")
      sig_col  <- if (is_short) RED else GREEN
      sig_icon <- if (is_short) "📉" else "📈"

      v <- calc_signal_pnl(s, ci$cap, ci$lev, ci$taker, ci$funding)

      str_col <- switch(s$strength,
        "Сильный"     = GREEN,
        "Прогнозный"  = ORANGE,
        "Формируется" = BLUE,
        GRAY)

      div(style = paste0(
        "border:2px solid ", sig_col, ";border-radius:14px;padding:18px 20px;",
        "margin-bottom:16px;background:", BG, ";box-shadow:0 0 20px ", sig_col, "22;"),
        div(style = paste0("font-size:1.15rem;font-weight:700;color:", sig_col, ";margin-bottom:14px;"),
          sig_icon, " ", s$signal),
        layout_columns(col_widths = c(3, 3, 3, 3),
          div(style = paste0("padding:12px;border-radius:8px;background:", CARD, ";border:1px solid ", BORDER, ";"),
            div(style = "font-size:0.75rem;color:#8b949e;margin-bottom:4px;", "Когда входить"),
            div(style = "font-size:0.95rem;font-weight:600;color:#e6edf3;", "Сейчас"),
            div(style = "font-size:0.78rem;color:#555;", paste0("Z = ", s$z_now))
          ),
          div(style = paste0("padding:12px;border-radius:8px;background:", CARD, ";border:1px solid ", BORDER, ";"),
            div(style = "font-size:0.75rem;color:#8b949e;margin-bottom:4px;", "Когда выходить"),
            div(style = "font-size:0.85rem;font-weight:600;color:#e6edf3;", "TP: Z → ±0.5"),
            div(style = "font-size:0.78rem;color:#555;", "SL: |Z| → 3.5")
          ),
          div(style = paste0("padding:12px;border-radius:8px;background:", CARD, ";border:1px solid ", BORDER, ";"),
            div(style = "font-size:0.75rem;color:#8b949e;margin-bottom:4px;", "Сколько держать"),
            div(style = "font-size:0.95rem;font-weight:600;color:#e6edf3;", v$hold_txt)
          ),
          div(style = paste0("padding:12px;border-radius:8px;background:", CARD, ";border:1px solid ", BORDER, ";"),
            div(style = "font-size:0.75rem;color:#8b949e;margin-bottom:4px;", "Сила сигнала"),
            div(style = paste0("font-size:0.95rem;font-weight:600;color:", str_col, ";"), s$strength),
            div(style = "font-size:0.72rem;color:#555;", v$src_txt)
          )
        ),
        calc_block_ui(v),
        div(style = "margin-top:12px;font-size:0.8rem;color:#8b949e;",
          badge(s$strength, str_col), "  ",
          if (isTRUE(s$is_coint)) "✅ Коинтегрированы" else "⚠️ Не коинтегрированы",
          "  ·  Корреляция: ", s$corr, "%",
          if (!is.na(s$halflife)) paste0("  ·  Полупериод: ", s$halflife, " дн.") else "")
      )
    })

    tagList(
      tags$div(style = "padding:12px 18px;border-radius:10px;border:1px solid #30363d;background:#0d1117;margin-bottom:18px;",
        tags$span(style = "color:#8b949e;font-size:0.85rem;",
          "Сигналы на основе Z-score спреда коинтегрированных пар. ",
          "Вход при |Z| ≥ 2, TP при |Z| < 0.5, SL при |Z| ≥ 3.5. ",
          "Профит — по истории backtest'а пары (или теоретический, если истории нет).")),
      tagList(cards)
    )
  })

  # ── ТАБ: Максимальный профит ────────────────────────────────────────────
  maxprofit_trades <- reactive({
    df <- pairs_coint()
    pw  <- price_wide()
    if (is.null(df) || is.null(pw)) return(NULL)

    # Correlation filter (slider)
    min_corr <- if (isTruthy(input$mp_min_corr)) input$mp_min_corr else 50
    good <- df[!is.na(df$corr) & abs(df$corr) >= min_corr / 100, , drop = FALSE]
    # Cointegration filter (optional checkbox)
    if (isTRUE(input$mp_coint_only)) {
      good <- good[!is.na(good$is_coint) & good$is_coint == TRUE, , drop = FALSE]
    }
    if (nrow(good) == 0) return(NULL)
    # Limit to top 50 pairs by score to avoid computing thousands of backtests
    good <- head(good[order(-good$score), ], 50)

    all_trades <- list()
    for (i in seq_len(nrow(good))) {
      r  <- good[i, ]
      hr <- if (!is.na(r$hedge_ratio)) r$hedge_ratio else 1
      th <- pair_trades_history(pw, r$A, r$B, hr)
      if (!is.null(th) && nrow(th) > 0) all_trades[[length(all_trades) + 1]] <- th
    }
    if (length(all_trades) == 0) return(NULL)
    tdf <- do.call(rbind, all_trades)
    tdf <- tdf[order(-tdf$pnl_pct), ]
    head(tdf, 20)
  })

  output$maxprofit_ui <- renderUI({
    tdf <- maxprofit_trades()
    if (is.null(tdf) || nrow(tdf) == 0)
      return(placeholder_msg("Нет сделок по выбранным фильтрам. Попробуйте снизить мин. корреляцию или выключить фильтр коинтеграции."))

    ci <- get_calc_inputs(input, "mpcalc_")
    pos_size <- ci$cap * ci$lev
    leg_size  <- pos_size / 2

    # Compute net P&L per trade (in USDT)
    tdf$gross  <- round(pos_size * tdf$pnl_pct / 100, 2)
    tdf$comm   <- round(4 * leg_size * ci$taker / 100, 2)
    tdf$funding <- round(pos_size * ci$funding / 100 * tdf$hold_days * 3, 2)
    tdf$net    <- round(tdf$gross - tdf$comm - tdf$funding, 2)

    # Summary stats
    total_net  <- round(sum(tdf$net), 2)
    avg_net    <- round(mean(tdf$net), 2)
    best_net   <- round(max(tdf$net), 2)
    n_win      <- sum(tdf$net > 0)

    fmt <- function(x) format(x, big.mark = " ", scientific = FALSE, trim = TRUE)

    # Build cards for top-5 trades
    top5 <- head(tdf, 5)
    cards <- lapply(seq_len(nrow(top5)), function(i) {
      r <- top5[i, ]
      pnl_col  <- if (r$net > 0) GREEN else RED
      res_col  <- if (r$result == "Тейк-профит") GREEN else RED
      res_icon <- if (r$result == "Тейк-профит") "✅" else "⛔"

      div(style = paste0(
        "border:1px solid ", BORDER, ";border-radius:14px;padding:16px 18px;",
        "margin-bottom:12px;background:", CARD, ";transition:all 0.3s;"),
        layout_columns(col_widths = c(5, 2, 2, 3),
          div(
            div(style = paste0("font-size:0.95rem;font-weight:700;color:", pnl_col, ";"),
              if (r$net > 0) "+" else "", "$", fmt(r$net), "  чистыми"),
            div(style = "font-size:0.82rem;color:#8b949e;margin-top:2px;", r$direction),
            div(style = "font-size:0.72rem;color:#555c6b;", r$pair)
          ),
          div(style = "text-align:center;",
            div(style = "font-size:0.72rem;color:#8b949e;", "Держали"),
            div(style = "font-size:1rem;font-weight:600;color:#e6edf3;", paste0(r$hold_days, "д")),
            div(style = "font-size:0.68rem;color:#555c6b;", paste0(r$entry_date, " → ", r$exit_date))
          ),
          div(style = "text-align:center;",
            div(style = "font-size:0.72rem;color:#8b949e;", "Результат"),
            div(style = paste0("font-size:0.9rem;font-weight:600;color:", res_col, ";"),
              res_icon, " ", r$result),
            div(style = "font-size:0.68rem;color:#555c6b;",
              paste0("Z: ", r$entry_z, " → ", r$exit_z))
          ),
          div(style = "text-align:right;",
            div(style = "font-size:0.72rem;color:#8b949e;", "Профит %"),
            div(style = paste0("font-size:1.3rem;font-weight:700;color:", pnl_col, ";"),
              paste0(if (r$pnl_pct > 0) "+" else "", r$pnl_pct, "%")),
            div(style = "font-size:0.68rem;color:#555c6b;",
              paste0("грязными: $", fmt(r$gross),
                     " | комиссии: -$", r$comm,
                     " | фонд: -$", r$funding))
          )
        )
      )
    })

    # Table for all 20 trades
    table_df <- tdf[, c("pair","direction","entry_date","exit_date","hold_days",
                        "pnl_pct","result","net")]
    colnames(table_df) <- c("Пара","Направление","Вход","Выход","Дней","Профит %","Итог","Чистыми $")

    tagList(
      # Summary banner
      div(style = paste0("padding:18px 22px;border-radius:14px;border:1px solid ", BORDER,
                         ";background:linear-gradient(135deg,", CARD, ",", CARD2,
                         ");margin-bottom:18px;"),
        layout_columns(col_widths = c(3, 3, 3, 3),
          div(style = "text-align:center;",
            div(style = "font-size:0.75rem;color:#8b949e;", "Топ-20 сделок"),
            div(style = paste0("font-size:1.5rem;font-weight:700;color:",
                               if (total_net > 0) GREEN else RED, ";"),
              paste0(if (total_net > 0) "+" else "", "$", fmt(total_net))),
            div(style = "font-size:0.68rem;color:#555c6b;", "суммарный профит")
          ),
          div(style = "text-align:center;",
            div(style = "font-size:0.75rem;color:#8b949e;", "Средняя сделка"),
            div(style = paste0("font-size:1.5rem;font-weight:700;color:",
                               if (avg_net > 0) GREEN else RED, ";"),
              paste0(if (avg_net > 0) "+" else "", "$", avg_net)),
            div(style = "font-size:0.68rem;color:#555c6b;", "чистыми")
          ),
          div(style = "text-align:center;",
            div(style = "font-size:0.75rem;color:#8b949e;", "Лучшая сделка"),
            div(style = "font-size:1.5rem;font-weight:700;color:#3fb950;",
              paste0("+$", fmt(best_net))),
            div(style = "font-size:0.68rem;color:#555c6b;", "чистыми")
          ),
          div(style = "text-align:center;",
            div(style = "font-size:0.75rem;color:#8b949e;", "Прибыльных"),
            div(style = paste0("font-size:1.5rem;font-weight:700;color:",
                               if (n_win >= 10) GREEN else ORANGE, ";"),
              paste0(n_win, "/20")),
            div(style = "font-size:0.68rem;color:#555c6b;", "из топ-20")
          )
        )
      ),
      # Info
      tags$div(style = "padding:12px 18px;border-radius:10px;border:1px solid #1c2333;background:#0a0e14;margin-bottom:18px;",
        tags$span(style = "color:#8b949e;font-size:0.82rem;",
          "Топ-20 самых прибыльных сделок за 3 года. ",
          "Стратегия: вход при |Z| ≥ 2, выход при |Z| < 0.5, стоп при |Z| ≥ 3.5. ",
          "Профит пересчитан в USDT с учётом комиссий MEXC и финансирования.")),
      # Top-5 cards
      tags$h6(style = "color:#e6edf3;margin-bottom:12px;font-size:0.88rem;", "🏆 Топ-5 сделок"),
      tagList(cards),
      # Full table
      tags$h6(style = "color:#e6edf3;margin:18px 0 12px;font-size:0.88rem;", "📋 Все топ-20 сделок"),
      DTOutput("maxprofit_table")
    )
  })

  output$maxprofit_table <- renderDT({
    tdf <- maxprofit_trades(); req(tdf)
    ci <- get_calc_inputs(input, "mpcalc_")
    pos_size <- ci$cap * ci$lev
    leg_size  <- pos_size / 2
    tdf$gross  <- round(pos_size * tdf$pnl_pct / 100, 2)
    tdf$comm   <- round(4 * leg_size * ci$taker / 100, 2)
    tdf$funding <- round(pos_size * ci$funding / 100 * tdf$hold_days * 3, 2)
    tdf$net    <- round(tdf$gross - tdf$comm - tdf$funding, 2)

    out <- data.frame(
      "Пара"        = tdf$pair,
      "Направление" = tdf$direction,
      "Вход"        = tdf$entry_date,
      "Выход"       = tdf$exit_date,
      "Дней"        = tdf$hold_days,
      "Профит %"    = tdf$pnl_pct,
      "Итог"        = tdf$result,
      "Чистыми $"   = tdf$net,
      stringsAsFactors = FALSE, check.names = FALSE
    )
    datatable(out, rownames = FALSE,
              options = list(pageLength = 20, dom = "tip", scrollX = TRUE,
                             order = list(list(5, "desc"))),
              style = "bootstrap5", class = "table-dark table-sm") |>
      formatStyle("Профит %",
        color = styleInterval(0, c("#f85149", "#3fb950")),
        fontWeight = "bold") |>
      formatStyle("Чистыми $",
        color = styleInterval(0, c("#f85149", "#3fb950")),
        fontWeight = "bold")
  })

  # ── ТАБ: Короткие сделки (до 7 дней) ────────────────────────────────────
  shorttrades_data <- reactive({
    df <- pairs_coint()
    pw  <- price_wide()
    if (is.null(df) || is.null(pw)) return(NULL)

    min_corr <- if (isTruthy(input$short_min_corr)) input$short_min_corr else 50
    max_days <- if (isTruthy(input$short_max_days)) input$short_max_days else 7
    good <- df[!is.na(df$corr) & abs(df$corr) >= min_corr / 100, , drop = FALSE]
    if (isTRUE(input$short_coint_only)) {
      good <- good[!is.na(good$is_coint) & good$is_coint == TRUE, , drop = FALSE]
    }
    if (nrow(good) == 0) return(NULL)
    good <- head(good[order(-good$score), ], 50)

    all_trades <- list()
    for (i in seq_len(nrow(good))) {
      r  <- good[i, ]
      hr <- if (!is.na(r$hedge_ratio)) r$hedge_ratio else 1
      th <- pair_trades_history(pw, r$A, r$B, hr)
      if (!is.null(th) && nrow(th) > 0) all_trades[[length(all_trades) + 1]] <- th
    }
    if (length(all_trades) == 0) return(NULL)
    tdf <- do.call(rbind, all_trades)
    # Filter: only trades with hold_days <= max_days
    tdf <- tdf[tdf$hold_days <= max_days, , drop = FALSE]
    if (nrow(tdf) == 0) return(NULL)
    tdf <- tdf[order(-tdf$pnl_pct), ]
    head(tdf, 20)
  })

  output$shorttrades_ui <- renderUI({
    tdf <- shorttrades_data()
    max_days <- if (isTruthy(input$short_max_days)) input$short_max_days else 7
    if (is.null(tdf) || nrow(tdf) == 0)
      return(placeholder_msg(paste0("Нет сделок до ", max_days, " дней по выбранным фильтрам.")))

    ci <- get_calc_inputs(input, "shortcalc_")
    pos_size <- ci$cap * ci$lev
    leg_size  <- pos_size / 2

    tdf$gross  <- round(pos_size * tdf$pnl_pct / 100, 2)
    tdf$comm   <- round(4 * leg_size * ci$taker / 100, 2)
    tdf$funding <- round(pos_size * ci$funding / 100 * tdf$hold_days * 3, 2)
    tdf$net    <- round(tdf$gross - tdf$comm - tdf$funding, 2)

    total_net  <- round(sum(tdf$net), 2)
    avg_net    <- round(mean(tdf$net), 2)
    best_net   <- round(max(tdf$net), 2)
    avg_hold   <- round(mean(tdf$hold_days), 1)
    n_win      <- sum(tdf$net > 0)
    n_total    <- nrow(tdf)

    fmt <- function(x) format(x, big.mark = " ", scientific = FALSE, trim = TRUE)

    top5 <- head(tdf, 5)
    cards <- lapply(seq_len(nrow(top5)), function(i) {
      r <- top5[i, ]
      pnl_col  <- if (r$net > 0) GREEN else RED
      res_col  <- if (r$result == "Тейк-профит") GREEN else RED
      res_icon <- if (r$result == "Тейк-профит") "✅" else "⛔"
      # Highlight very short trades
      is_fast  <- r$hold_days <= 3
      speed_col <- if (is_fast) ORANGE else BLUE

      div(style = paste0(
        "border:1px solid ", BORDER, ";border-radius:14px;padding:16px 18px;",
        "margin-bottom:12px;background:", CARD, ";transition:all 0.3s;"),
        layout_columns(col_widths = c(5, 2, 2, 3),
          div(
            div(style = paste0("font-size:0.95rem;font-weight:700;color:", pnl_col, ";"),
              if (r$net > 0) "+" else "", "$", fmt(r$net), "  чистыми"),
            div(style = "font-size:0.82rem;color:#8b949e;margin-top:2px;", r$direction),
            div(style = "font-size:0.72rem;color:#555c6b;", r$pair)
          ),
          div(style = "text-align:center;",
            div(style = "font-size:0.72rem;color:#8b949e;", "Держали"),
            div(style = paste0("font-size:1rem;font-weight:600;color:", speed_col, ";"), paste0(r$hold_days, "д")),
            div(style = "font-size:0.68rem;color:#555c6b;", paste0(r$entry_date, " → ", r$exit_date))
          ),
          div(style = "text-align:center;",
            div(style = "font-size:0.72rem;color:#8b949e;", "Результат"),
            div(style = paste0("font-size:0.9rem;font-weight:600;color:", res_col, ";"),
              res_icon, " ", r$result),
            div(style = "font-size:0.68rem;color:#555c6b;",
              paste0("Z: ", r$entry_z, " → ", r$exit_z))
          ),
          div(style = "text-align:right;",
            div(style = "font-size:0.72rem;color:#8b949e;", "Профит %"),
            div(style = paste0("font-size:1.3rem;font-weight:700;color:", pnl_col, ";"),
              paste0(if (r$pnl_pct > 0) "+" else "", r$pnl_pct, "%")),
            div(style = "font-size:0.68rem;color:#555c6b;",
              paste0("грязными: $", fmt(r$gross),
                     " | комиссии: -$", r$comm,
                     " | фонд: -$", r$funding))
          )
        )
      )
    })

    tagList(
      # Summary
      div(style = paste0("padding:18px 22px;border-radius:14px;border:1px solid ", BORDER,
                         ";background:linear-gradient(135deg,", CARD, ",", CARD2,
                         ");margin-bottom:18px;"),
        layout_columns(col_widths = c(3, 3, 3, 3),
          div(style = "text-align:center;",
            div(style = "font-size:0.75rem;color:#8b949e;", paste0("Сделок до ", max_days, " дн.")),
            div(style = "font-size:1.5rem;font-weight:700;color:#e6edf3;", n_total),
            div(style = "font-size:0.68rem;color:#555c6b;", paste0("сред. удержание ", avg_hold, " дн."))
          ),
          div(style = "text-align:center;",
            div(style = "font-size:0.75rem;color:#8b949e;", "Суммарный профит"),
            div(style = paste0("font-size:1.5rem;font-weight:700;color:",
                               if (total_net > 0) GREEN else RED, ";"),
              paste0(if (total_net > 0) "+" else "", "$", fmt(total_net))),
            div(style = "font-size:0.68rem;color:#555c6b;", "чистыми")
          ),
          div(style = "text-align:center;",
            div(style = "font-size:0.75rem;color:#8b949e;", "Лучшая сделка"),
            div(style = "font-size:1.5rem;font-weight:700;color:#3fb950;",
              paste0("+$", fmt(best_net))),
            div(style = "font-size:0.68rem;color:#555c6b;", "чистыми")
          ),
          div(style = "text-align:center;",
            div(style = "font-size:0.75rem;color:#8b949e;", "Прибыльных"),
            div(style = paste0("font-size:1.5rem;font-weight:700;color:",
                               if (n_win >= n_total / 2) GREEN else ORANGE, ";"),
              paste0(n_win, "/", n_total)),
            div(style = "font-size:0.68rem;color:#555c6b;", "коротких сделок")
          )
        )
      ),
      # Info
      tags$div(style = "padding:12px 18px;border-radius:10px;border:1px solid #1c2333;background:#0a0e14;margin-bottom:18px;",
        tags$span(style = "color:#8b949e;font-size:0.82rem;",
          "Топ-20 самых прибыльных коротких сделок (до ", max_days, " дней). ",
          "Быстрый возврат спреда к среднему = эффективнее капитала. ",
          "Меньше финансирование, но выше риск шума. ",
          "Оранжевым отмечены сделки ≤ 3 дней (очень быстрые).")),
      # Top-5
      tags$h6(style = "color:#e6edf3;margin-bottom:12px;font-size:0.88rem;", "⚡ Топ-5 коротких сделок"),
      tagList(cards),
      # Table
      tags$h6(style = "color:#e6edf3;margin:18px 0 12px;font-size:0.88rem;", "📋 Все короткие сделки"),
      DTOutput("shorttrades_table")
    )
  })

  output$shorttrades_table <- renderDT({
    tdf <- shorttrades_data(); req(tdf)
    ci <- get_calc_inputs(input, "shortcalc_")
    pos_size <- ci$cap * ci$lev
    leg_size  <- pos_size / 2
    tdf$gross  <- round(pos_size * tdf$pnl_pct / 100, 2)
    tdf$comm   <- round(4 * leg_size * ci$taker / 100, 2)
    tdf$funding <- round(pos_size * ci$funding / 100 * tdf$hold_days * 3, 2)
    tdf$net    <- round(tdf$gross - tdf$comm - tdf$funding, 2)

    out <- data.frame(
      "Пара"        = tdf$pair,
      "Направление" = tdf$direction,
      "Вход"        = tdf$entry_date,
      "Выход"       = tdf$exit_date,
      "Дней"        = tdf$hold_days,
      "Профит %"    = tdf$pnl_pct,
      "Итог"        = tdf$result,
      "Чистыми $"   = tdf$net,
      stringsAsFactors = FALSE, check.names = FALSE
    )
    datatable(out, rownames = FALSE,
              options = list(pageLength = 20, dom = "tip", scrollX = TRUE,
                             order = list(list(5, "desc"))),
              style = "bootstrap5", class = "table-dark table-sm") |>
      formatStyle("Профит %",
        color = styleInterval(0, c("#f85149", "#3fb950")),
        fontWeight = "bold") |>
      formatStyle("Чистыми $",
        color = styleInterval(0, c("#f85149", "#3fb950")),
        fontWeight = "bold") |>
      formatStyle("Дней",
        color = styleInterval(3, c("#f7931a", "#58a6ff")))
  })

  # ══════════════════════════════════════════════════════════════════════════
  # ТАБ: Сканеры — 4 алгоритма для поиска быстрых зависимостей
  # ══════════════════════════════════════════════════════════════════════════

  # ── 1. Lead-Lag: кто кого опережает на 1-3 дня ──────────────────────────
  leadlag_scan <- reactive({
    pw <- price_wide(); req(pw)
    if (ncol(pw) < 2) return(NULL)
    rw <- as.data.frame(lapply(pw, function(x) c(NA, diff(log(as.numeric(x))))))
    coins <- colnames(rw)
    pairs <- combn(coins, 2, simplify = FALSE)

    res <- list()
    for (p in pairs) {
      xa <- rw[[p[1]]]; xb <- rw[[p[2]]]
      ok <- !is.na(xa) & !is.na(xb)
      if (sum(ok) < 60) next
      cc <- tryCatch(ccf(xa[ok], xb[ok], lag.max = 7, plot = FALSE), error = function(e) NULL)
      if (is.null(cc)) next
      lags <- as.numeric(cc$lag); acfs <- as.numeric(cc$acf)
      ci <- qnorm(0.975) / sqrt(sum(ok))
      sig <- which(abs(acfs) > ci & lags != 0)
      if (length(sig) == 0) next
      best_idx <- sig[which.max(abs(acfs[sig]))]
      best_lag <- lags[best_idx]; best_acf <- acfs[best_idx]
      if (abs(best_lag) > 5) next  # too slow

      leader   <- if (best_lag > 0) p[1] else p[2]
      follower <- if (best_lag > 0) p[2] else p[1]
      direction <- if (best_acf > 0) "двигаются в одну сторону" else "двигаются в разные стороны"

      # Today's leader return → signal for follower
      leader_ret <- tail(rw[[leader]][!is.na(rw[[leader]])], 1)
      follower_action <- if (best_acf > 0) {
        if (leader_ret > 0) paste0("📈 Лонг ", follower) else paste0("📉 Шорт ", follower)
      } else {
        if (leader_ret > 0) paste0("📉 Шорт ", follower) else paste0("📈 Лонг ", follower)
      }

      res[[length(res) + 1]] <- data.frame(
        leader = leader, follower = follower, lag = abs(best_lag),
        acf = round(best_acf, 3), direction = direction,
        leader_today = round(leader_ret * 100, 2),
        signal = follower_action,
        stringsAsFactors = FALSE)
    }
    if (length(res) == 0) return(NULL)
    df <- do.call(rbind, res)
    df <- df[order(-abs(df$acf)), ]
    head(df, 15)
  })

  # ── 2. Mean Reversion: Z-score цены от скользящего среднего ──────────────
  meanrev_scan <- reactive({
    pw <- price_wide(); req(pw)
    if (ncol(pw) < 2) return(NULL)
    ma_period <- 50

    res <- list()
    for (sym in colnames(pw)) {
      x <- as.numeric(pw[[sym]])
      x <- x[!is.na(x)]
      if (length(x) < ma_period + 10) next
      ma <- zoo::rollmean(x, ma_period, align = "right", fill = NA)
      sd_roll <- zoo::rollapply(x, ma_period, sd, align = "right", fill = NA)
      last_idx <- length(x)
      ma_now <- ma[last_idx]; sd_now <- sd_roll[last_idx]; price_now <- x[last_idx]
      if (is.na(ma_now) || is.na(sd_now) || sd_now == 0) next
      z <- (price_now - ma_now) / sd_now
      if (abs(z) < 1.5) next  # only show significant deviations

      signal <- if (z > 0) paste0("📉 Шорт ", sym, " (перегрет, отскок вниз)")
                else paste0("📈 Лонг ", sym, " (перепродан, отскок вверх)")

      res[[length(res) + 1]] <- data.frame(
        ticker = sym, price = round(price_now, 4),
        ma50 = round(ma_now, 4), z_score = round(z, 2),
        deviation = paste0(round(abs(z) * sd_now / ma_now * 100, 1), "%"),
        signal = signal,
        stringsAsFactors = FALSE)
    }
    if (length(res) == 0) return(NULL)
    df <- do.call(rbind, res)
    df <- df[order(-abs(df$z_score)), ]
    df
  })

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

  # ── 5. Anomaly: аномальные движения + их зависимости ────────────────────
  anomaly_scan <- reactive({
    pw <- price_wide(); req(pw)
    if (ncol(pw) < 2) return(NULL)
    rw <- as.data.frame(lapply(pw, function(x) c(NA, diff(log(as.numeric(x))))))
    lookback <- 90  # days for baseline stats

    # Step 1: find anomalies (|Z| > 2 on latest return vs rolling 90d stats)
    anomalies <- list()
    for (sym in colnames(rw)) {
      x <- rw[[sym]]
      x_clean <- x[!is.na(x)]
      if (length(x_clean) < lookback + 5) next
      hist_x <- tail(head(x_clean, length(x_clean) - 1), lookback)
      mu <- mean(hist_x); sd_x <- sd(hist_x)
      if (is.na(sd_x) || sd_x == 0) next
      last_ret <- tail(x_clean, 1)
      z <- (last_ret - mu) / sd_x
      if (abs(z) < 2) next  # only anomalies
      anomalies[[length(anomalies) + 1]] <- data.frame(
        ticker = sym, ret_pct = round(last_ret * 100, 2),
        z_score = round(z, 2), direction = if (z > 0) "📈 Аномальный рост" else "📉 Аномальное падение",
        stringsAsFactors = FALSE)
    }
    if (length(anomalies) == 0) return(NULL)
    anom_df <- do.call(rbind, anomalies)
    anom_df <- anom_df[order(-abs(anom_df$z_score)), ]

    # Step 2: for each anomaly, find followers (lag 1-3 on extreme moves)
    followers <- list()
    anom_tickers <- anom_df$ticker
    other_tickers <- setdiff(colnames(rw), anom_tickers)
    if (length(other_tickers) > 0 && length(anom_tickers) > 0) {
      for (a in anom_tickers) {
        xa <- rw[[a]]
        for (b in other_tickers) {
          xb <- rw[[b]]
          ok <- !is.na(xa) & !is.na(xb)
          if (sum(ok) < 60) next
          xa_c <- xa[ok]; xb_c <- xb[ok]
          # Lag xa by 1-3 days and check correlation on extreme moves only
          for (lag in 1:3) {
            if (length(xa_c) <= lag + 10) next
            xa_lag <- head(xa_c, -lag)
            xb_l   <- tail(xb_c, -lag)
            if (length(xa_lag) < 30) next
            # Correlation on extreme moves (|xa_lag| > 1.5sd)
            sd_a <- sd(xa_lag)
            extreme <- abs(xa_lag) > 1.5 * sd_a
            if (sum(extreme) < 5) next
            c_ext <- cor(xa_lag[extreme], xb_l[extreme])
            if (is.na(c_ext) || abs(c_ext) < 0.4) next
            followers[[length(followers) + 1]] <- data.frame(
              anomaly = a, follower = b, lag = lag,
              ext_corr = round(c_ext, 2),
              anomaly_dir = if (anom_df$z_score[anom_df$ticker == a] > 0) "рост" else "падение",
              follower_action = if (c_ext > 0) {
                if (anom_df$z_score[anom_df$ticker == a] > 0) paste0("📈 Лонг ", b, " (после роста ", a, ")")
                else paste0("📉 Шорт ", b, " (после падения ", a, ")")
              } else {
                if (anom_df$z_score[anom_df$ticker == a] > 0) paste0("📉 Шорт ", b, " (после роста ", a, ")")
                else paste0("📈 Лонг ", b, " (после падения ", a, ")")
              },
              stringsAsFactors = FALSE)
          }
        }
      }
    }

    list(anomalies = anom_df,
         followers = if (length(followers) > 0) do.call(rbind, followers) else NULL)
  })

  # ── 6. Volume: аномалии объёма ──────────────────────────────────────────
  volume_scan <- reactive({
    df <- raw_data(); req(df)
    if (!"volume_col" %in% colnames(df)) {
      # DB data has no volume column; fetch from prices directly
      pw <- price_wide(); req(pw)
      return(NULL)
    }
    vol_df <- df[!is.na(df$volume_col) & df$volume_col > 0, , drop = FALSE]
    if (nrow(vol_df) == 0) return(NULL)
    lookback <- 30
    res <- list()
    for (sym in unique(vol_df$ticker_col)) {
      sub <- vol_df[vol_df$ticker_col == sym, ]
      sub <- sub[order(sub$date), ]
      if (nrow(sub) < lookback + 5) next
      vols <- sub$volume_col
      hist_v <- tail(head(vols, length(vols) - 1), lookback)
      mu <- mean(hist_v); sd_v <- sd(hist_v)
      if (is.na(sd_v) || sd_v == 0) next
      last_v <- tail(vols, 1)
      z <- (last_v - mu) / sd_v
      if (abs(z) < 1.5) next
      last_price <- tail(sub$price_col, 1)
      prev_price <- sub$price_col[nrow(sub) - 1]
      price_chg <- (last_price / prev_price - 1) * 100
      res[[length(res) + 1]] <- data.frame(
        ticker = sym, volume_z = round(z, 2),
        volume = format(last_v, big.mark = " ", scientific = FALSE),
        avg_volume = format(round(mu), big.mark = " ", scientific = FALSE),
        price_chg = round(price_chg, 2),
        signal = if (z > 2 && price_chg > 3) paste0("📈 Лонг ", sym, " (объём + рост = институционал вход)")
                 else if (z > 2 && price_chg < -3) paste0("📉 Шорт ", sym, " (объём + падение = институционал выход)")
                 else if (z > 1.5) paste0("⚠ Высокий объём ", sym, " — наблюдать")
                 else paste0("⚠ Низкий объём ", sym, " — нет интереса"),
        stringsAsFactors = FALSE)
    }
    if (length(res) == 0) return(NULL)
    df <- do.call(rbind, res)
    df[order(-abs(df$volume_z)), ]
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

  # ── 8. Multi-TF: Z-score на 7/30/90 днях одновременно ───────────────────
  multitf_scan <- reactive({
    pw <- price_wide(); req(pw)
    if (ncol(pw) < 2) return(NULL)
    res <- list()
    for (sym in colnames(pw)) {
      x <- as.numeric(pw[[sym]])
      x <- x[!is.na(x)]
      if (length(x) < 95) next
      calc_z <- function(period) {
        recent <- tail(x, period)
        ma <- mean(recent)
        sd_v <- sd(recent)
        if (is.na(sd_v) || sd_v == 0) return(NA)
        (tail(x, 1) - ma) / sd_v
      }
      z7  <- calc_z(7)
      z30 <- calc_z(30)
      z90 <- calc_z(90)
      if (any(is.na(c(z7, z30, z90)))) next
      # All same direction and |z| > 1 on all timeframes
      same_dir <- (sign(z7) == sign(z30) && sign(z30) == sign(z90))
      all_sig  <- (abs(z7) > 1 && abs(z30) > 1 && abs(z90) > 1)
      if (!same_dir || !all_sig) next
      n_sig <- sum(abs(c(z7, z30, z90)) > 2)
      res[[length(res) + 1]] <- data.frame(
        ticker = sym, z7 = round(z7, 2), z30 = round(z30, 2), z90 = round(z90, 2),
        confirmed = n_sig, stringsAsFactors = FALSE)
    }
    if (length(res) == 0) return(NULL)
    df <- do.call(rbind, res)
    df$signal <- ifelse(df$z90 > 0,
      paste0("📉 Шорт ", df$ticker, " (перегрет на всех TF)"),
      paste0("📈 Лонг ", df$ticker, " (перепродан на всех TF)"))
    df[order(-df$confirmed), ]
  })

  # ── 9. Volatility: режим волатильности рынка ────────────────────────────
  volatility_scan <- reactive({
    pw <- price_wide(); req(pw)
    if (ncol(pw) < 2) return(NULL)
    rw <- as.data.frame(lapply(pw, function(x) c(NA, diff(log(as.numeric(x))))))
    vols <- sapply(rw, function(x) {
      x <- x[!is.na(x)]
      if (length(x) < 30) return(NA)
      sd(tail(x, 30)) * sqrt(365) * 100  # annualized %
    })
    vols <- vols[!is.na(vols)]
    if (length(vols) == 0) return(NULL)
    avg_vol <- mean(vols)
    # Historical avg vol (last 90 days of returns for whole market)
    recent_rw <- rw[nrow(rw) - 89:0, , drop = FALSE]
    hist_vols <- sapply(recent_rw, function(x) {
      x <- x[!is.na(x)]
      if (length(x) < 20) return(NA)
      sd(x) * sqrt(365) * 100
    })
    hist_vols <- hist_vols[!is.na(hist_vols)]
    hist_avg <- if (length(hist_vols) > 0) mean(hist_vols) else avg_vol
    regime <- if (avg_vol > hist_avg * 1.5) "🔥 Высокая (бурный)"
              else if (avg_vol > hist_avg * 1.1) "⚠ Повышенная"
              else if (avg_vol < hist_avg * 0.7) "🧊 Низкая (спокойный)"
              else "✅ Нормальная"
    per_asset <- data.frame(
      ticker = names(vols), vol = round(vols, 1),
      stringsAsFactors = FALSE)
    per_asset <- per_asset[order(-per_asset$vol), ]
    list(regime = regime, avg_vol = round(avg_vol, 1),
         hist_avg = round(hist_avg, 1), ratio = round(avg_vol / hist_avg, 2),
         per_asset = per_asset)
  })

  # ── Scanner UI dispatcher ────────────────────────────────────────────────
  output$scanner_ui <- renderUI({
    st <- input$scanner_type
    if (is.null(st)) return(NULL)

    if (st == "leadlag") {
      df <- leadlag_scan()
      if (is.null(df) || nrow(df) == 0)
        return(placeholder_msg("Не найдено опережений на 1-5 дней по этому рынку."))
      ci <- get_calc_inputs(input, "scancalc_")
      cards <- lapply(seq_len(min(6, nrow(df))), function(i) {
        r <- df[i, ]
        lag_col <- if (r$lag <= 2) GREEN else if (r$lag <= 3) ORANGE else BLUE
        # Build signal for calculator: hold ~ lag days, tp from |leader_today|
        s <- list(
          signal_type = if (grepl("Шорт", r$signal)) "short_a" else "long_a",
          z_now = r$leader_today / 10,  # approx Z from % move
          halflife = r$lag, bt = NULL, strength = "Сканер")
        v <- calc_signal_pnl(s, ci$cap, ci$lev, ci$taker, ci$funding)
        tags$div(style = paste0(
          "border:1px solid ", BORDER, ";border-radius:14px;padding:14px 16px;",
          "margin-bottom:10px;background:", CARD, ";"),
          layout_columns(col_widths = c(5, 3, 4),
            div(
              tags$span(style = "font-size:1rem;font-weight:700;color:#e6edf3;",
                r$leader, " → ", r$follower),
              tags$br(),
              tags$span(style = "font-size:0.8rem;color:#8b949e;", r$direction)
            ),
            div(style = "text-align:center;",
              tags$div(style = "font-size:0.72rem;color:#8b949e;", "Опережение"),
              tags$div(style = paste0("font-size:1.2rem;font-weight:700;color:", lag_col, ";"),
                paste0(r$lag, " дн.")),
              tags$div(style = "font-size:0.68rem;color:#555c6b;",
                paste0("сила: ", abs(r$acf)))
            ),
            div(style = "text-align:right;",
              tags$div(style = "font-size:0.72rem;color:#8b949e;",
                paste0("Лидер сегодня: ", if (r$leader_today > 0) "▲" else "▼", " ", r$leader_today, "%")),
              tags$div(style = "font-size:0.9rem;font-weight:600;color:#58a6ff;", r$signal)
            )
          ),
          calc_block_ui(v)
        )
      })
      tagList(
        tags$div(style = "padding:12px 18px;border-radius:10px;border:1px solid #1c2333;background:#0a0e14;margin-bottom:18px;",
          tags$span(style = "color:#8b949e;font-size:0.85rem;",
            "Если A опережает B на N дней, движение A сегодня = сигнал для B завтра. ",
            "Зелёное опережение (1-2 дня) — самый быстрый сигнал. ",
            "Сила = кросс-корреляция (ACF), чем выше, тем надёжнее.")),
        tagList(cards),
        tags$h6(style = "color:#e6edf3;margin:18px 0 12px;", "📋 Все пары с опережением"),
        DTOutput("leadlag_table")
      )

    } else if (st == "meanrev") {
      df <- meanrev_scan()
      if (is.null(df) || nrow(df) == 0)
        return(placeholder_msg("Нет инструментов с сильным отклонением от среднего (|Z| > 1.5)."))
      ci <- get_calc_inputs(input, "scancalc_")
      cards <- lapply(seq_len(min(6, nrow(df))), function(i) {
        r <- df[i, ]
        z_col <- if (abs(r$z_score) >= 2) RED else ORANGE
        s <- list(
          signal_type = if (r$z_score > 0) "short_a" else "long_a",
          z_now = r$z_score, halflife = 5, bt = NULL, strength = "Сканер")
        v <- calc_signal_pnl(s, ci$cap, ci$lev, ci$taker, ci$funding)
        tags$div(style = paste0(
          "border:1px solid ", BORDER, ";border-radius:14px;padding:14px 16px;",
          "margin-bottom:10px;background:", CARD, ";"),
          layout_columns(col_widths = c(3, 3, 3, 3),
            div(
              tags$div(style = "font-size:0.95rem;font-weight:700;color:#e6edf3;", r$ticker),
              tags$div(style = "font-size:0.72rem;color:#555c6b;", "цена")
            ),
            div(style = "text-align:center;",
              tags$div(style = "font-size:0.72rem;color:#8b949e;", "Цена"),
              tags$div(style = "font-size:0.9rem;font-weight:600;color:#e6edf3;", r$price)
            ),
            div(style = "text-align:center;",
              tags$div(style = "font-size:0.72rem;color:#8b949e;", "MA50"),
              tags$div(style = "font-size:0.9rem;font-weight:600;color:#8b949e;", r$ma50),
              tags$div(style = "font-size:0.68rem;color:#555c6b;", paste0("откл: ", r$deviation))
            ),
            div(style = "text-align:right;",
              tags$div(style = "font-size:0.72rem;color:#8b949e;", "Z-score"),
              tags$div(style = paste0("font-size:1.3rem;font-weight:700;color:", z_col, ";"), r$z_score),
              tags$div(style = "font-size:0.72rem;font-weight:500;color:#58a6ff;", r$signal)
            )
          ),
          calc_block_ui(v)
        )
      })
      tagList(
        tags$div(style = "padding:12px 18px;border-radius:10px;border:1px solid #1c2333;background:#0a0e14;margin-bottom:18px;",
          tags$span(style = "color:#8b949e;font-size:0.85rem;",
            "Инструменты, сильно отклонившиеся от 50-дневного среднего. ",
            "Z > 2 — перегрет (шорт), Z < -2 — перепродан (лонг). ",
            "Отскок обычно за 1-5 дней.")),
        tagList(cards),
        tags$h6(style = "color:#e6edf3;margin:18px 0 12px;", "📋 Все отклонения"),
        DTOutput("meanrev_table")
      )

    } else if (st == "corrbreak") {
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

    } else if (st == "anomaly") {
      result <- anomaly_scan()
      if (is.null(result)) return(placeholder_msg("Нет аномальных движений (|Z| > 2) на этом рынке."))
      anom <- result$anomalies
      foll <- result$followers
      ci <- get_calc_inputs(input, "scancalc_")

      # Anomaly cards
      anom_cards <- lapply(seq_len(min(6, nrow(anom))), function(i) {
        r <- anom[i, ]
        z_col <- if (abs(r$z_score) >= 3) RED else ORANGE
        s <- list(
          signal_type = if (r$z_score > 0) "short_a" else "long_a",
          z_now = r$z_score, halflife = 3, bt = NULL, strength = "Аномалия")
        v <- calc_signal_pnl(s, ci$cap, ci$lev, ci$taker, ci$funding)
        tags$div(style = paste0(
          "border:2px solid ", z_col, ";border-radius:14px;padding:14px 16px;",
          "margin-bottom:10px;background:", CARD, ";box-shadow:0 0 20px ", z_col, "22;"),
          layout_columns(col_widths = c(4, 3, 3, 2),
            div(
              tags$div(style = paste0("font-size:1rem;font-weight:700;color:", z_col, ";"), r$direction),
              tags$div(style = "font-size:0.85rem;color:#e6edf3;font-weight:600;", r$ticker)
            ),
            div(style = "text-align:center;",
              tags$div(style = "font-size:0.72rem;color:#8b949e;", "Движение"),
              tags$div(style = paste0("font-size:1.1rem;font-weight:700;color:",
                                       if (r$ret_pct > 0) GREEN else RED, ";"),
                paste0(if (r$ret_pct > 0) "+" else "", r$ret_pct, "%"))
            ),
            div(style = "text-align:center;",
              tags$div(style = "font-size:0.72rem;color:#8b949e;", "Z-score"),
              tags$div(style = paste0("font-size:1.3rem;font-weight:700;color:", z_col, ";"),
                r$z_score),
              tags$div(style = "font-size:0.68rem;color:#555c6b;", "vs 90д")
            ),
            div(style = "text-align:right;",
              tags$div(style = "font-size:0.72rem;color:#8b949e;", "Реакция"),
              tags$div(style = "font-size:0.78rem;color:#58a6ff;",
                if (r$z_score > 0) "Шорт (откат)" else "Лонг (отскок)")
            )
          ),
          calc_block_ui(v)
        )
      })

      # Follower cards (dependencies)
      foll_cards <- NULL
      if (!is.null(foll) && nrow(foll) > 0) {
        foll <- foll[order(-abs(foll$ext_corr)), ]
        foll_top <- head(foll, 8)
        foll_cards <- lapply(seq_len(nrow(foll_top)), function(i) {
          r <- foll_top[i, ]
          corr_col <- if (abs(r$ext_corr) >= 0.6) GREEN else if (abs(r$ext_corr) >= 0.5) ORANGE else BLUE
          tags$div(style = paste0(
            "border:1px solid ", BORDER, ";border-radius:14px;padding:14px 16px;",
            "margin-bottom:10px;background:", CARD, ";"),
            layout_columns(col_widths = c(5, 2, 2, 3),
              div(
                tags$span(style = "font-size:0.95rem;font-weight:700;color:#e6edf3;",
                  r$anomaly, " → ", r$follower),
                tags$br(),
                tags$span(style = "font-size:0.78rem;color:#8b949e;",
                  paste0("После аномального ", r$anomaly_dir, " ", r$anomaly))
              ),
              div(style = "text-align:center;",
                tags$div(style = "font-size:0.72rem;color:#8b949e;", "Лаг"),
                tags$div(style = "font-size:1rem;font-weight:700;color:#f7931a;",
                  paste0(r$lag, " дн."))
              ),
              div(style = "text-align:center;",
                tags$div(style = "font-size:0.72rem;color:#8b949e;", "Корр. на экстремумах"),
                tags$div(style = paste0("font-size:1rem;font-weight:700;color:", corr_col, ";"),
                  r$ext_corr)
              ),
              div(style = "text-align:right;",
                tags$div(style = "font-size:0.72rem;color:#8b949e;", "Сигнал"),
                tags$div(style = "font-size:0.82rem;font-weight:600;color:#58a6ff;",
                  r$follower_action)
              )
            )
          )
        })
      }

      tagList(
        tags$div(style = "padding:12px 18px;border-radius:10px;border:1px solid #1c2333;background:#0a0e14;margin-bottom:18px;",
          tags$span(style = "color:#8b949e;font-size:0.85rem;",
            "Аномалии = движения с |Z| > 2 относительно своей 90-дневной истории. ",
            "Z > 3 (красный) — экстрим, возможен быстрый откат. ",
            "Ниже — зависимости: инструменты, которые исторически реагировали на аномалии с лагом 1-3 дня.")),
        tags$h6(style = "color:#e6edf3;margin-bottom:12px;font-size:0.88rem;",
          paste0("🚨 Аномальные движения сегодня: ", nrow(anom), " инструментов")),
        tagList(anom_cards),
        if (!is.null(foll_cards)) tagList(
          tags$h6(style = "color:#e6edf3;margin:18px 0 12px;font-size:0.88rem;",
            "🔗 Зависимости: кто реагирует на аномалии с лагом"),
          tagList(foll_cards)
        ) else if (nrow(anom) > 0) tags$div(
          style = "padding:14px;color:#555c6b;font-size:0.82rem;text-align:center;",
          "Зависимостей с лагом 1-3 дня не найдено для текущих аномалий."),
        tags$h6(style = "color:#e6edf3;margin:18px 0 12px;", "📋 Все аномалии"),
        DTOutput("anomaly_table"),
        if (!is.null(foll) && nrow(foll) > 0) tagList(
          tags$h6(style = "color:#e6edf3;margin:18px 0 12px;", "📋 Все зависимости"),
          DTOutput("anomaly_followers_table"))
      )

    } else if (st == "volume") {
      df <- volume_scan()
      if (is.null(df) || nrow(df) == 0)
        return(placeholder_msg("Нет данных по объёму или аномалий не найдено."))
      ci <- get_calc_inputs(input, "scancalc_")
      cards <- lapply(seq_len(min(6, nrow(df))), function(i) {
        r <- df[i, ]
        z_col <- if (abs(r$volume_z) >= 3) RED else ORANGE
        is_signal <- grepl("Лонг|Шорт", r$signal)
        s <- list(signal_type = if (grepl("Шорт", r$signal)) "short_a" else "long_a",
                  z_now = r$volume_z / 2, halflife = 5, bt = NULL, strength = "Volume")
        v <- if (is_signal) calc_signal_pnl(s, ci$cap, ci$lev, ci$taker, ci$funding) else NULL
        div(style = paste0("border:1px solid ", BORDER, ";border-radius:14px;padding:14px 16px;margin-bottom:10px;background:", CARD, ";"),
          layout_columns(col_widths = c(3, 3, 2, 4),
            div(tags$div(style="font-size:1rem;font-weight:700;color:#e6edf3;", r$ticker),
                tags$div(style="font-size:0.72rem;color:#555c6b;", paste0("vol: ", r$volume))),
            div(style="text-align:center;",
              tags$div(style="font-size:0.72rem;color:#8b949e;", "Объём Z"),
              tags$div(style=paste0("font-size:1.2rem;font-weight:700;color:", z_col, ";"), r$volume_z),
              tags$div(style="font-size:0.68rem;color:#555c6b;", paste0("сред: ", r$avg_volume))),
            div(style="text-align:center;",
              tags$div(style="font-size:0.72rem;color:#8b949e;", "Цена"),
              tags$div(style=paste0("font-size:1rem;font-weight:600;color:",
                                     if(r$price_chg>0) GREEN else RED, ";"),
                paste0(if(r$price_chg>0)"+"else"", r$price_chg, "%"))),
            div(style="text-align:right;",
              tags$div(style="font-size:0.78rem;font-weight:500;color:#58a6ff;", r$signal))
          ),
          if (!is.null(v)) calc_block_ui(v)
        )
      })
      tagList(
        tags$div(style="padding:12px 18px;border-radius:10px;border:1px solid #1c2333;background:#0a0e14;margin-bottom:18px;",
          tags$span(style="color:#8b949e;font-size:0.85rem;",
            "Аномалии объёма (|Z| > 1.5). Объём + рост = институционал вход. Объём + падение = институционал выход. ",
            "Высокий объём без движения — накопление/распределение.")),
        tagList(cards),
        tags$h6(style="color:#e6edf3;margin:18px 0 12px;", "📋 Все аномалии объёма"),
        DTOutput("volume_table"))

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

    } else if (st == "multitf") {
      df <- multitf_scan()
      if (is.null(df) || nrow(df) == 0)
        return(placeholder_msg("Нет подтверждённых сигналов на всех таймфреймах (7/30/90 дней)."))
      ci <- get_calc_inputs(input, "scancalc_")
      cards <- lapply(seq_len(min(6, nrow(df))), function(i) {
        r <- df[i, ]
        all3 <- r$confirmed == 3
        z_col <- if (all3) RED else ORANGE
        s <- list(signal_type = if (r$z90 > 0) "short_a" else "long_a",
                  z_now = r$z90, halflife = 10, bt = NULL, strength = "Multi-TF")
        v <- calc_signal_pnl(s, ci$cap, ci$lev, ci$taker, ci$funding)
        div(style = paste0("border:1px solid ", z_col, ";border-radius:14px;padding:14px 16px;margin-bottom:10px;background:", CARD, ";"),
          layout_columns(col_widths = c(3, 2, 2, 2, 3),
            div(tags$div(style="font-size:1rem;font-weight:700;color:#e6edf3;", r$ticker),
                tags$div(style="font-size:0.72rem;color:#555c6b;",
                  if(all3) "3/3 подтверждено" else paste0(r$confirmed, "/3 подтверждено"))),
            div(style="text-align:center;",
              tags$div(style="font-size:0.72rem;color:#8b949e;", "Z 7д"),
              tags$div(style=paste0("font-size:0.95rem;font-weight:600;color:",
                                     if(abs(r$z7)>2) RED else ORANGE, ";"), r$z7)),
            div(style="text-align:center;",
              tags$div(style="font-size:0.72rem;color:#8b949e;", "Z 30д"),
              tags$div(style=paste0("font-size:0.95rem;font-weight:600;color:",
                                     if(abs(r$z30)>2) RED else ORANGE, ";"), r$z30)),
            div(style="text-align:center;",
              tags$div(style="font-size:0.72rem;color:#8b949e;", "Z 90д"),
              tags$div(style=paste0("font-size:0.95rem;font-weight:600;color:",
                                     if(abs(r$z90)>2) RED else ORANGE, ";"), r$z90)),
            div(style="text-align:right;",
              tags$div(style="font-size:0.72rem;color:#8b949e;", "Сигнал"),
              tags$div(style="font-size:0.78rem;font-weight:500;color:#58a6ff;", r$signal))
          ),
          calc_block_ui(v)
        )
      })
      tagList(
        tags$div(style="padding:12px 18px;border-radius:10px;border:1px solid #1c2333;background:#0a0e14;margin-bottom:18px;",
          tags$span(style="color:#8b949e;font-size:0.85rem;",
            "Z-score цены на 7/30/90 днях одновременно. Все 3 в одну сторону + |Z| > 1 = сильный сигнал. ",
            "3/3 подтверждено (|Z| > 2 на всех TF) = максимальная надёжность.")),
        tagList(cards),
        tags$h6(style="color:#e6edf3;margin:18px 0 12px;", "📋 Все multi-TF сигналы"),
        DTOutput("multitf_table"))

    } else if (st == "volatility") {
      res <- volatility_scan()
      if (is.null(res)) return(placeholder_msg("Нет данных по волатильности."))
      regime_col <- if (grepl("Высокая|бурный", res$regime)) RED
                    else if (grepl("Повышенная", res$regime)) ORANGE
                    else if (grepl("Низкая|спокойный", res$regime)) BLUE
                    else GREEN

      # Detailed strategy recommendations per regime
      if (grepl("Высокая|бурный", res$regime)) {
        recs <- list(
          list("🎯 Mean Reversion — приоритет", "Быстрые возвраты после всплесков. Вход при |Z|>2, выход за 1-3 дня.", GREEN),
          list("⚠ Pairs Trading — отлично", "Спреды расходятся сильнее = больше сигналов. Коинтеграция устойчива.", GREEN),
          list("🚫 Momentum — опасно", "Тренды ломаются резко. Не держи дольше 2-3 дней.", RED),
          list("📏 Размер позиции — уменьшить", "Высокая вола = широкий стоп. Сократи размер на 30-50%.", ORANGE))
      } else if (grepl("Повышенная", res$regime)) {
        recs <- list(
          list("🎯 Mean Reversion — хорошо", "Возвраты работают, но медленнее. Hold 3-7 дней.", GREEN),
          list("🤝 Pairs Trading — нормально", "Сигналы есть, но слабее. Фильтруй по |Z|>2.", ORANGE),
          list("⚠ Momentum — осторожно", "Тренды есть, но хрупкие. Стоп-лосс обязателен.", ORANGE),
          list("📏 Размер позиции — стандарт", "Обычный риск, без изменений.", GRAY))
      } else if (grepl("Низкая|спокойный", res$regime)) {
        recs <- list(
          list("🚀 Momentum — приоритет", "Тренды устойчивые, держатся долго. Hold 7-14 дней.", GREEN),
          list("🚫 Mean Reversion — слабо", "Отклонения маленькие, возвраты мелкие. |Z| редко > 2.", RED),
          list("🤝 Pairs Trading — мало сигналов", "Спреды узкие, входы редкие. Жди расширения.", ORANGE),
          list("📏 Размер позиции — можно увеличить", "Низкая вола = узкий стоп. Размер можно +20-30%.", GREEN))
      } else {
        recs <- list(
          list("✅ Все стратегии в норме", "Mean reversion и momentum работают одинаково.", GRAY),
          list("🤝 Pairs Trading — стабильно", "Обычное количество сигналов, стандартный hold.", GRAY),
          list("📏 Размер позиции — стандарт", "Обычный риск.", GRAY))
      }

      rec_cards <- lapply(recs, function(r) {
        div(style = paste0("display:flex;align-items:flex-start;gap:12px;padding:12px 16px;",
                           "border-radius:10px;background:", CARD2, ";margin-bottom:8px;",
                           "border-left:3px solid ", r[[3]], ";"),
          div(style = paste0("font-size:0.88rem;font-weight:600;color:", r[[3]], ";min-width:200px;"),
            r[[1]]),
          div(style = "font-size:0.82rem;color:#adbac7;flex:1;",
            r[[2]]))
      })

      top_vol <- head(res$per_asset, 10)
      vol_bars <- lapply(seq_len(nrow(top_vol)), function(i) {
        r <- top_vol[i, ]
        bar_w <- min(r$vol / max(top_vol$vol) * 100, 100)
        div(style = paste0("display:flex;align-items:center;gap:10px;margin-bottom:6px;"),
          div(style = "font-size:0.82rem;color:#e6edf3;width:90px;font-weight:600;", r$ticker),
          div(style = paste0("height:20px;border-radius:4px;width:", bar_w, "%;",
                             "background:linear-gradient(90deg,#58a6ff,#a78bfa);"),
            div(style = "font-size:0.72rem;color:#e6edf3;padding:2px 8px;line-height:16px;",
              paste0(r$vol, "%"))))
      })

      tagList(
        # Regime banner
        div(style = paste0("padding:18px 22px;border-radius:14px;border:2px solid ", regime_col,
                           ";background:", CARD, ";margin-bottom:18px;text-align:center;"),
          div(style = "font-size:0.8rem;color:#8b949e;margin-bottom:6px;", "Текущий режим рынка"),
          div(style = paste0("font-size:1.6rem;font-weight:700;color:", regime_col, ";margin-bottom:8px;"),
            res$regime),
          layout_columns(col_widths = c(3, 3, 3, 3),
            div(style="text-align:center;",
              div(style="font-size:0.72rem;color:#8b949e;", "Текущая волат."),
              div(style="font-size:1.2rem;font-weight:700;color:#e6edf3;", paste0(res$avg_vol, "%"))),
            div(style="text-align:center;",
              div(style="font-size:0.72rem;color:#8b949e;", "Историческая"),
              div(style="font-size:1.2rem;font-weight:700;color:#8b949e;", paste0(res$hist_avg, "%"))),
            div(style="text-align:center;",
              div(style="font-size:0.72rem;color:#8b949e;", "Отношение"),
              div(style=paste0("font-size:1.2rem;font-weight:700;color:", regime_col, ";"), res$ratio)),
            div(style="text-align:center;",
              div(style="font-size:0.72rem;color:#8b949e;", "Период"),
              div(style="font-size:1.2rem;font-weight:700;color:#e6edf3;", "30 дней"))
          )
        ),
        # Recommendations
        div(style = "padding:18px 22px;border-radius:14px;border:1px solid #1c2333;background:#0f1419;margin-bottom:18px;",
          div(style = "font-size:1rem;font-weight:700;color:#e6edf3;margin-bottom:14px;",
            "📋 Рекомендации по стратегиям"),
          tagList(rec_cards)),
        # Top volatile
        tags$h6(style = "color:#e6edf3;margin-bottom:12px;", "📊 Топ-10 по волатильности (годовой %)"),
        tagList(vol_bars),
        tags$h6(style = "color:#e6edf3;margin:18px 0 12px;", "📋 Все инструменты"),
        DTOutput("volatility_table"))
    }
  })

  # ── Scanner tables ───────────────────────────────────────────────────────
  output$leadlag_table <- renderDT({
    df <- leadlag_scan(); req(df)
    datatable(data.frame(
      "Лидер" = df$leader, "Ведомый" = df$follower,
      "Опережение (дн.)" = df$lag, "Сила (ACF)" = df$acf,
      "Связь" = df$direction, "Лидер сегодня %" = df$leader_today,
      "Сигнал" = df$signal,
      stringsAsFactors = FALSE, check.names = FALSE),
      rownames = FALSE, options = list(pageLength = 15, dom = "tip", scrollX = TRUE),
      style = "bootstrap5", class = "table-dark table-sm")
  })

  output$meanrev_table <- renderDT({
    df <- meanrev_scan(); req(df)
    datatable(data.frame(
      "Тикер" = df$ticker, "Цена" = df$price, "MA50" = df$ma50,
      "Z-score" = df$z_score, "Отклонение" = df$deviation,
      "Сигнал" = df$signal,
      stringsAsFactors = FALSE, check.names = FALSE),
      rownames = FALSE, options = list(pageLength = 15, dom = "tip", scrollX = TRUE),
      style = "bootstrap5", class = "table-dark table-sm") |>
      formatStyle("Z-score",
        color = styleInterval(c(-2, 2), c("#3fb950", "#f7931a", "#f85149")),
        fontWeight = "bold")
  })

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

  output$anomaly_table <- renderDT({
    res <- anomaly_scan(); req(res)
    df <- res$anomalies
    datatable(data.frame(
      "Тикер" = df$ticker, "Движение %" = df$ret_pct,
      "Z-score" = df$z_score, "Тип" = df$direction,
      stringsAsFactors = FALSE, check.names = FALSE),
      rownames = FALSE, options = list(pageLength = 15, dom = "tip", scrollX = TRUE),
      style = "bootstrap5", class = "table-dark table-sm") |>
      formatStyle("Z-score",
        color = styleInterval(c(-3, -2, 2, 3), c("#3fb950", "#f7931a", "#f7931a", "#f85149", "#f85149")),
        fontWeight = "bold") |>
      formatStyle("Движение %",
        color = styleInterval(0, c("#f85149", "#3fb950")),
        fontWeight = "bold")
  })

  output$anomaly_followers_table <- renderDT({
    res <- anomaly_scan(); req(res, res$followers)
    df <- res$followers
    datatable(data.frame(
      "Аномалия" = df$anomaly, "Ведомый" = df$follower,
      "Лаг (дн.)" = df$lag, "Корр. на экстремумах" = df$ext_corr,
      "После" = df$anomaly_dir, "Сигнал" = df$follower_action,
      stringsAsFactors = FALSE, check.names = FALSE),
      rownames = FALSE, options = list(pageLength = 15, dom = "tip", scrollX = TRUE),
      style = "bootstrap5", class = "table-dark table-sm") |>
      formatStyle("Корр. на экстремумах",
        color = styleInterval(c(-0.6, -0.4, 0.4, 0.6),
          c("#3fb950", "#f7931a", "#f7931a", "#58a6ff", "#3fb950")),
        fontWeight = "bold")
  })

  output$volume_table <- renderDT({
    df <- volume_scan(); req(df)
    datatable(data.frame(
      "Тикер" = df$ticker, "Volume Z" = df$volume_z,
      "Объём" = df$volume, "Средний объём" = df$avg_volume,
      "Цена %" = df$price_chg, "Сигнал" = df$signal,
      stringsAsFactors = FALSE, check.names = FALSE),
      rownames = FALSE, options = list(pageLength = 15, dom = "tip", scrollX = TRUE),
      style = "bootstrap5", class = "table-dark table-sm") |>
      formatStyle("Volume Z",
        color = styleInterval(c(-2, 2), c("#58a6ff", "#f7931a", "#f85149")),
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

  output$multitf_table <- renderDT({
    df <- multitf_scan(); req(df)
    datatable(data.frame(
      "Тикер" = df$ticker, "Z 7д" = df$z7, "Z 30д" = df$z30,
      "Z 90д" = df$z90, "Подтверждено" = df$confirmed,
      "Сигнал" = df$signal,
      stringsAsFactors = FALSE, check.names = FALSE),
      rownames = FALSE, options = list(pageLength = 15, dom = "tip", scrollX = TRUE),
      style = "bootstrap5", class = "table-dark table-sm")
  })

  output$volatility_table <- renderDT({
    res <- volatility_scan(); req(res)
    df <- res$per_asset
    datatable(data.frame(
      "Тикер" = df$ticker, "Волатильность %/год" = df$vol,
      stringsAsFactors = FALSE, check.names = FALSE),
      rownames = FALSE, options = list(pageLength = 15, dom = "tip", scrollX = TRUE),
      style = "bootstrap5", class = "table-dark table-sm")
  })

  # ══════════════════════════════════════════════════════════════════════════
  # ТАБ: Паттерны — календарные закономерности
  # ══════════════════════════════════════════════════════════════════════════
  patterns_data <- reactive({
    df <- raw_data(); req(df)
    if (nrow(df) < 100) return(NULL)
    df$dow <- as.integer(format(df$date, "%w"))  # 0=Sun, 6=Sat
    df$dom <- as.integer(format(df$date, "%d"))  # day of month
    df$ret <- c(NA, diff(log(df$price_col)))  # per ticker, need careful

    # Per-ticker daily returns
    res <- list()
    for (sym in unique(df$ticker_col)) {
      sub <- df[df$ticker_col == sym, ]
      sub <- sub[order(sub$date), ]
      sub$ret <- c(NA, diff(log(sub$price_col)))
      sub <- sub[!is.na(sub$ret), ]
      if (nrow(sub) < 60) next

      # By day of week
      dow_stats <- tapply(sub$ret, sub$dow, function(x) c(mean(x) * 100, length(x)))
      dow_names <- c("Вс", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб")
      dow_avg <- sapply(0:6, function(d) {
        vals <- sub$ret[sub$dow == d]
        if (length(vals) < 5) return(NA)
        mean(vals) * 100
      })
      dow_n <- sapply(0:6, function(d) sum(sub$dow == d))

      # End of month (last 3 trading days)
      sub$is_eom <- sub$dom >= 28
      eom_avg <- if (sum(sub$is_eom) > 5) mean(sub$ret[sub$is_eom]) * 100 else NA
      non_eom_avg <- if (sum(!sub$is_eom) > 5) mean(sub$ret[!sub$is_eom]) * 100 else NA

      # Find best day
      best_dow <- which.max(dow_avg) - 1
      worst_dow <- which.min(dow_avg) - 1
      best_avg <- dow_avg[best_dow + 1]
      worst_avg <- dow_avg[worst_dow + 1]

      if (any(is.na(dow_avg))) next

      # Signal: best day to buy/sell
      best_day_name <- dow_names[best_dow + 1]
      worst_day_name <- dow_names[worst_dow + 1]
      signal <- if (best_avg > 0 && best_avg > 0.3)
        paste0("📈 Лонг ", sym, " по ", best_day_name, " (средний +", round(best_avg, 2), "%)")
        else if (worst_avg < -0.3)
        paste0("📉 Шорт ", sym, " по ", worst_day_name, " (средний ", round(worst_avg, 2), "%)")
        else paste0("— Нет явного календарного паттерна у ", sym)

      res[[length(res) + 1]] <- data.frame(
        ticker = sym,
        best_day = best_day_name, best_avg = round(best_avg, 2),
        worst_day = worst_day_name, worst_avg = round(worst_avg, 2),
        eom_avg = round(eom_avg, 2), non_eom_avg = round(non_eom_avg, 2),
        signal = signal,
        stringsAsFactors = FALSE)
    }
    if (length(res) == 0) return(NULL)
    do.call(rbind, res)
  })

  output$patterns_ui <- renderUI({
    df <- patterns_data()
    if (is.null(df) || nrow(df) == 0)
      return(placeholder_msg("Недостаточно данных для календарных паттернов."))

    # Filter: only show actionable patterns
    actionable <- df[grepl("Лонг|Шорт", df$signal), , drop = FALSE]
    if (nrow(actionable) == 0)
      return(div(style = "text-align:center;padding:40px;color:#8b949e;",
        tags$i(class = "fas fa-calendar-check fa-3x", style = "color:#30363d;margin-bottom:12px;"),
        p("Явных календарных паттернов не найдено."),
        p(style = "font-size:0.82rem;color:#555c6b;",
          "Ни один день недели не даёт статистически значимого преимущества.")))

    cards <- lapply(seq_len(min(8, nrow(actionable))), function(i) {
      r <- actionable[i, ]
      best_col <- if (r$best_avg > 0.3) GREEN else GRAY
      worst_col <- if (r$worst_avg < -0.3) RED else GRAY
      eom_col <- if (!is.na(r$eom_avg) && r$eom_avg > r$non_eom_avg) ORANGE else GRAY

      div(style = paste0("border:1px solid ", BORDER, ";border-radius:14px;padding:14px 16px;margin-bottom:10px;background:", CARD, ";"),
        layout_columns(col_widths = c(4, 3, 3, 2),
          div(
            tags$div(style = "font-size:1rem;font-weight:700;color:#e6edf3;", r$ticker),
            tags$div(style = "font-size:0.78rem;color:#58a6ff;font-weight:500;", r$signal)),
          div(style = "text-align:center;",
            tags$div(style = "font-size:0.72rem;color:#8b949e;", paste0("Лучший день: ", r$best_day)),
            tags$div(style = paste0("font-size:1.1rem;font-weight:700;color:", best_col, ";"),
              paste0("+", r$best_avg, "%")),
            tags$div(style = "font-size:0.68rem;color:#555c6b;", "средний return")),
          div(style = "text-align:center;",
            tags$div(style = "font-size:0.72rem;color:#8b949e;", paste0("Худший день: ", r$worst_day)),
            tags$div(style = paste0("font-size:1.1rem;font-weight:700;color:", worst_col, ";"),
              paste0(r$worst_avg, "%")),
            tags$div(style = "font-size:0.68rem;color:#555c6b;", "средний return")),
          div(style = "text-align:right;",
            tags$div(style = "font-size:0.72rem;color:#8b949e;", "Конец месяца"),
            tags$div(style = paste0("font-size:0.95rem;font-weight:600;color:", eom_col, ";"),
              if (is.na(r$eom_avg)) "—" else paste0(r$eom_avg, "%")),
            tags$div(style = "font-size:0.68rem;color:#555c6b;",
              if (is.na(r$non_eom_avg)) "" else paste0("vs ", r$non_eom_avg, "%")))
        )
      )
    })

    tagList(
      tags$div(style = "padding:12px 18px;border-radius:10px;border:1px solid #1c2333;background:#0a0e14;margin-bottom:18px;",
        tags$span(style = "color:#8b949e;font-size:0.85rem;",
          "Календарные паттерны: день недели, конец месяца. ",
          "Показаны только инструменты с статистически значимым преимуществом (|return| > 0.3%). ",
          "Сделка: 1 день (купить вечером перед лучшим днём, продать вечером лучшего дня).")),
      tagList(cards),
      tags$h6(style = "color:#e6edf3;margin:18px 0 12px;", "📋 Все инструменты"),
      DTOutput("patterns_table"))
  })

  output$patterns_table <- renderDT({
    df <- patterns_data(); req(df)
    datatable(data.frame(
      "Тикер" = df$ticker, "Лучший день" = df$best_day, "Avg %" = df$best_avg,
      "Худший день" = df$worst_day, "Avg %" = df$worst_avg,
      "Конец месяца %" = df$eom_avg, "Остальные %" = df$non_eom_avg,
      "Сигнал" = df$signal,
      stringsAsFactors = FALSE, check.names = FALSE),
      rownames = FALSE, options = list(pageLength = 20, dom = "tip", scrollX = TRUE),
      style = "bootstrap5", class = "table-dark table-sm")
  })

  # ══════════════════════════════════════════════════════════════════════════
  # ТАБ: Риск — корреляция портфеля + liquidity sweep
  # ══════════════════════════════════════════════════════════════════════════
  risk_data <- reactive({
    pw <- price_wide(); req(pw)
    if (ncol(pw) < 2) return(NULL)
    rw <- as.data.frame(lapply(pw, function(x) c(NA, diff(log(as.numeric(x))))))
    cor_mat <- cor(rw, use = "pairwise.complete.obs")

    # Liquidity sweep: price crossed 20-day high/low but reverted
    sweeps <- list()
    for (sym in colnames(pw)) {
      x <- as.numeric(pw[[sym]])
      x <- x[!is.na(x)]
      if (length(x) < 30) next
      last <- tail(x, 1)
      prev <- x[length(x) - 1]
      window <- tail(head(x, length(x) - 1), 20)
      high <- max(window)
      low <- min(window)
      # Sweep up: prev broke high, last reverted below
      sweep_up <- prev > high && last < high && last < prev
      # Sweep down: prev broke low, last reverted above
      sweep_down <- prev < low && last > low && last > prev
      if (!sweep_up && !sweep_down) next
      sweep_type <- if (sweep_up) "Ложный пробой вверх" else "Ложный пробой вниз"
      signal <- if (sweep_up) paste0("📉 Шорт ", sym, " (ложный пробой вверх — откат)")
                else paste0("📈 Лонг ", sym, " (ложный пробой вниз — отскок)")
      sweeps[[length(sweeps) + 1]] <- data.frame(
        ticker = sym, type = sweep_type,
        level = if (sweep_up) round(high, 4) else round(low, 4),
        current = round(last, 4), signal = signal,
        stringsAsFactors = FALSE)
    }
    sweeps_df <- if (length(sweeps) > 0) do.call(rbind, sweeps) else NULL

    # High-correlation clusters (for portfolio risk)
    high_corr <- which(upper.tri(cor_mat) & abs(cor_mat) > 0.7, arr.ind = TRUE)
    clusters <- if (nrow(high_corr) > 0) {
      data.frame(
        A = colnames(cor_mat)[high_corr[, 1]],
        B = colnames(cor_mat)[high_corr[, 2]],
        corr = round(cor_mat[high_corr], 2),
        stringsAsFactors = FALSE)
    } else NULL

    list(cor_mat = cor_mat, sweeps = sweeps_df, clusters = clusters)
  })

  output$risk_ui <- renderUI({
    res <- risk_data()
    if (is.null(res)) return(placeholder_msg("Недостаточно данных для анализа риска."))

    # Correlation heatmap
    cor_html <- NULL
    if (!is.null(res$cor_mat) && ncol(res$cor_mat) >= 2) {
      cm <- res$cor_mat
      # Top 12 by variance to keep heatmap readable
      if (ncol(cm) > 12) {
        vars <- apply(cm, 2, function(x) sum(x^2, na.rm = TRUE))
        cm <- cm[order(-vars)[1:12], order(-vars)[1:12]]
      }
      n <- ncol(cm)
      cells <- ""
      for (i in seq_len(n)) {
        for (j in seq_len(n)) {
          v <- cm[i, j]
          if (i == j) {
            col <- "#1c2333"
          } else if (is.na(v)) {
            col <- "#0a0e14"
          } else if (v > 0) {
            intensity <- abs(v)
            col <- sprintf("rgba(88,166,255,%.2f)", intensity)
          } else {
            intensity <- abs(v)
            col <- sprintf("rgba(248,81,73,%.2f)", intensity)
          }
          cells <- paste0(cells, sprintf(
            '<div style="background:%s;width:28px;height:28px;display:flex;align-items:center;justify-content:center;font-size:0.55rem;color:%s;">%s</div>',
            col, if (abs(v) > 0.5 && !is.na(v)) "#fff" else "#555",
            if (is.na(v)) "" else sprintf("%.1f", v)))
        }
        cells <- paste0(cells, '<div style="width:100%;"></div>')
      }
      labels <- paste0(sprintf('<div style="width:28px;text-align:center;font-size:0.5rem;color:#8b949e;writing-mode:vertical-rl;transform:rotate(180deg);height:60px;">%s</div>',
        colnames(cm)), collapse = "")
      cor_html <- div(
        div(style = "display:flex;gap:2px;margin-bottom:4px;padding-left:60px;", lapply(colnames(cm), function(x)
          div(style = "width:28px;text-align:center;font-size:0.5rem;color:#8b949e;transform:rotate(-45deg);height:40px;", x))),
        div(style = "display:flex;gap:2px;",
          div(style = "display:flex;flex-direction:column;gap:2px;margin-right:4px;",
            lapply(colnames(cm), function(x) div(style = "height:28px;display:flex;align-items:center;justify-content:flex-end;font-size:0.55rem;color:#8b949e;width:56px;", x))),
          div(style = "display:flex;flex-wrap:wrap;gap:2px;width:", 28 * n, "px;", HTML(cells))))
    }

    # Sweep cards
    sweep_cards <- NULL
    if (!is.null(res$sweeps) && nrow(res$sweeps) > 0) {
      sweep_cards <- lapply(seq_len(min(6, nrow(res$sweeps))), function(i) {
        r <- res$sweeps[i, ]
        sweep_col <- if (grepl("Шорт", r$signal)) RED else GREEN
        div(style = paste0("border:1px solid ", sweep_col, ";border-radius:14px;padding:14px 16px;margin-bottom:10px;background:", CARD, ";"),
          layout_columns(col_widths = c(4, 3, 3, 2),
            div(tags$div(style="font-size:1rem;font-weight:700;color:#e6edf3;", r$ticker),
                tags$div(style="font-size:0.78rem;color:#8b949e;", r$type)),
            div(style="text-align:center;",
              tags$div(style="font-size:0.72rem;color:#8b949e;", "Уровень"),
              tags$div(style="font-size:0.9rem;font-weight:600;color:#8b949e;", r$level)),
            div(style="text-align:center;",
              tags$div(style="font-size:0.72rem;color:#8b949e;", "Текущая"),
              tags$div(style="font-size:0.9rem;font-weight:600;color:#e6edf3;", r$current)),
            div(style="text-align:right;",
              tags$div(style="font-size:0.78rem;font-weight:500;color:", sweep_col, ";", r$signal))
          ))
      })
    }

    # Cluster warning
    cluster_block <- NULL
    if (!is.null(res$clusters) && nrow(res$clusters) > 0) {
      n_clusters <- nrow(res$clusters)
      cluster_block <- div(style = paste0("padding:16px 18px;border-radius:12px;border:1px solid ",
        if (n_clusters > 10) RED else if (n_clusters > 5) ORANGE else GREEN,
        ";background:", CARD, ";margin-bottom:18px;"),
        div(style = "font-size:0.9rem;font-weight:600;color:#e6edf3;margin-bottom:8px;",
          "⚠ Корреляция между активами"),
        p(style = "font-size:0.82rem;color:#8b949e;margin-bottom:10px;",
          paste0("Найдено ", n_clusters, " пар с корреляцией > 70%. ",
                 "Открытие позиций по нескольким из них = риск x2+ на одно событие. ",
                 "Диверсифицируй: не более 1 пары из каждого кластера.")),
        DTOutput("risk_clusters_table"))
    }

    tagList(
      tags$div(style = "padding:12px 18px;border-radius:10px;border:1px solid #1c2333;background:#0a0e14;margin-bottom:18px;",
        tags$span(style = "color:#8b949e;font-size:0.85rem;",
          "Анализ риска портфеля: корреляционная матрица, ложные пробои (liquidity sweep), ",
          "кластеры корреляции для управления диверсификацией.")),
      # Liquidity sweep
      tags$h6(style = "color:#e6edf3;margin-bottom:12px;font-size:0.88rem;",
        "🎯 Liquidity Sweep — ложные пробои"),
      if (!is.null(sweep_cards)) tagList(sweep_cards) else div(
        style = "padding:20px;text-align:center;color:#555c6b;font-size:0.85rem;",
        "Ложных пробоев 20-дневных уровней сегодня не найдено."),
      # Correlation heatmap
      tags$h6(style = "color:#e6edf3;margin:18px 0 12px;font-size:0.88rem;",
        "🔥 Корреляционная матрица доходностей"),
      if (!is.null(cor_html)) cor_html else div(
        style = "padding:20px;text-align:center;color:#555c6b;font-size:0.85rem;",
        "Недостаточно инструментов для матрицы."),
      # Clusters
      tags$h6(style = "color:#e6edf3;margin:18px 0 12px;font-size:0.88rem;",
        "📊 Кластеры высокой корреляции (>70%)"),
      if (!is.null(cluster_block)) cluster_block else div(
        style = "padding:20px;text-align:center;color:#3fb950;font-size:0.85rem;",
        "✅ Высоких корреляций не найдено — портфель хорошо диверсифицирован.")
    )
  })

  output$risk_clusters_table <- renderDT({
    res <- risk_data(); req(res, res$clusters)
    df <- res$clusters
    datatable(data.frame(
      "A" = df$A, "B" = df$B, "Корреляция" = df$corr,
      "Риск" = ifelse(abs(df$corr) > 0.8, "🔴 Критический",
               ifelse(abs(df$corr) > 0.7, "🟡 Высокий", "🟢 Умеренный")),
      stringsAsFactors = FALSE, check.names = FALSE),
      rownames = FALSE, options = list(pageLength = 15, dom = "tip", scrollX = TRUE),
      style = "bootstrap5", class = "table-dark table-sm") |>
      formatStyle("Корреляция",
        color = styleInterval(c(-0.8, -0.7, 0.7, 0.8),
          c("#f85149", "#f7931a", "#f7931a", "#f7931a", "#f85149")),
        fontWeight = "bold")
  })

  # ══════════════════════════════════════════════════════════════════════════
  # ТАБ: Топ-6 монет — глубокие паттерны XRP, SOL, BTC, ETH, DOGE, BNB
  # ══════════════════════════════════════════════════════════════════════════
  TOP6_COINS <- c("BTC/USD", "ETH/USD", "BNB/USD", "SOL/USD", "XRP/USD", "DOGE/USD")

  top6_data <- reactive({
    pw <- price_wide(); req(pw)
    available <- intersect(TOP6_COINS, colnames(pw))
    if (length(available) < 2) return(NULL)

    res <- list()
    for (sym in available) {
      x <- as.numeric(pw[[sym]])
      dates <- as.Date(rownames(pw))
      ok <- !is.na(x) & x > 0
      x <- x[ok]; dates <- dates[ok]
      if (length(x) < 100) next
      ret <- c(NA, diff(log(x)))
      dow <- as.integer(format(dates, "%w"))
      dom <- as.integer(format(dates, "%d"))

      dow_names <- c("Вс", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб")
      dow_avg <- sapply(0:6, function(d) {
        vals <- ret[dow == d]
        if (length(vals) < 5) return(NA)
        mean(vals, na.rm = TRUE) * 100
      })
      dow_win <- sapply(0:6, function(d) {
        vals <- ret[dow == d]
        vals <- vals[!is.na(vals)]
        if (length(vals) < 5) return(NA)
        round(sum(vals > 0) / length(vals) * 100)
      })
      dow_n <- sapply(0:6, function(d) sum(dow == d & !is.na(ret)))

      # End of month
      is_eom <- dom >= 28
      eom_ret <- ret[is_eom]
      eom_avg <- if (sum(!is.na(eom_ret)) > 5) mean(eom_ret, na.rm = TRUE) * 100 else NA
      eom_win <- if (sum(!is.na(eom_ret)) > 5) round(sum(eom_ret > 0, na.rm = TRUE) / sum(!is.na(eom_ret)) * 100) else NA
      non_eom_ret <- ret[!is_eom]
      non_eom_avg <- if (sum(!is.na(non_eom_ret)) > 5) mean(non_eom_ret, na.rm = TRUE) * 100 else NA

      # Consecutive up/down streaks
      ret_clean <- ret[!is.na(ret)]
      streaks <- rle(ret_clean > 0)
      up_streaks <- streaks$lengths[streaks$values]
      down_streaks <- streaks$lengths[!streaks$values]
      avg_up <- if (length(up_streaks) > 0) round(mean(up_streaks), 1) else NA
      max_up <- if (length(up_streaks) > 0) max(up_streaks) else NA
      avg_down <- if (length(down_streaks) > 0) round(mean(down_streaks), 1) else NA
      max_down <- if (length(down_streaks) > 0) max(down_streaks) else NA
      current_streak <- tail(streaks$lengths, 1) * ifelse(tail(streaks$values, 1), 1, -1)

      # Best/worst day
      best_idx <- which.max(dow_avg)
      worst_idx <- which.min(dow_avg)
      best_day <- dow_names[best_idx]
      worst_day <- dow_names[worst_idx]
      best_avg <- dow_avg[best_idx]
      worst_avg <- dow_avg[worst_idx]
      best_win <- dow_win[best_idx]
      worst_win <- dow_win[worst_idx]

      # Monthly seasonality (avg return per month)
      month <- as.integer(format(dates, "%m"))
      month_names <- c("Янв","Фев","Мар","Апр","Май","Июн","Июл","Авг","Сен","Окт","Ноя","Дек")
      month_avg <- sapply(1:12, function(m) {
        vals <- ret[month == m]
        if (length(vals) < 3) return(NA)
        mean(vals, na.rm = TRUE) * 100
      })

      # Today's day name
      today_dow <- as.integer(format(Sys.Date(), "%w"))
      today_name <- dow_names[today_dow + 1]
      today_avg <- dow_avg[today_dow + 1]
      today_win <- dow_win[today_dow + 1]

      # Current month
      today_month <- as.integer(format(Sys.Date(), "%m"))
      current_month_avg <- month_avg[today_month]

      res[[sym]] <- list(
        sym = sym, dow_avg = dow_avg, dow_win = dow_win, dow_n = dow_n,
        dow_names = dow_names, best_day = best_day, best_avg = best_avg,
        best_win = best_win, worst_day = worst_day, worst_avg = worst_avg,
        worst_win = worst_win, eom_avg = eom_avg, eom_win = eom_win,
        non_eom_avg = non_eom_avg, avg_up = avg_up, max_up = max_up,
        avg_down = avg_down, max_down = max_down, current_streak = current_streak,
        month_avg = month_avg, month_names = month_names,
        today_name = today_name, today_avg = today_avg, today_win = today_win,
        current_month_avg = current_month_avg,
        last_price = tail(x, 1), n_days = length(x))
    }
    res
  })

  output$top6_ui <- renderUI({
    data <- top6_data()
    if (is.null(data) || length(data) == 0)
      return(placeholder_msg("Топ-6 монет не найдены в данных. Выбран рынок crypto?"))

    # Build per-coin cards
    cards <- lapply(data, function(d) {
      # Day-of-week bars
      dow_bars <- lapply(1:7, function(i) {
        avg <- d$dow_avg[i]
        win <- d$dow_win[i]
        n <- d$dow_n[i]
        if (is.na(avg)) return(div(style="height:24px;"))
        col <- if (avg > 0.2) GREEN else if (avg < -0.2) RED else GRAY
        bar_w <- min(abs(avg) / 2 * 100, 100)
        is_today <- d$dow_names[i] == d$today_name
        border <- if (is_today) paste0("border:2px solid ", BLUE, ";") else "border:1px solid #1c2333;"
        div(style = paste0("display:flex;align-items:center;gap:8px;margin-bottom:4px;padding:4px 8px;border-radius:6px;background:", CARD2, ";", border),
          div(style = "font-size:0.72rem;color:#adbac7;width:24px;font-weight:600;", d$dow_names[i]),
          div(style = paste0("font-size:0.78rem;font-weight:600;color:", col, ";width:55px;"),
            paste0(if (avg > 0) "+" else "", round(avg, 2), "%")),
          div(style = paste0("height:14px;border-radius:3px;width:", bar_w, "%;background:", col, ";min-width:4px;")),
          div(style = "font-size:0.66rem;color:#555c6b;margin-left:auto;", paste0(win, "% win · ", n, " дн.")))
      })

      # Monthly seasonality mini-bars
      month_bars <- lapply(1:12, function(i) {
        avg <- d$month_avg[i]
        if (is.na(avg)) return(div(style="font-size:0.55rem;color:#333;width:22px;text-align:center;","—"))
        col <- if (avg > 0.2) GREEN else if (avg < -0.2) RED else GRAY
        is_current <- d$month_names[i] == d$month_names[as.integer(format(Sys.Date(), "%m"))]
        div(style = paste0("text-align:center;width:24px;"),
          div(style = paste0("font-size:0.55rem;color:#555c6b;"), substr(d$month_names[i], 1, 1)),
          div(style = paste0("font-size:0.6rem;font-weight:600;color:", col, ";",
                             if (is_current) paste0("border-bottom:2px solid ", BLUE, ";") else ""),
            paste0(if (avg > 0) "+" else "", round(avg, 1))))
      })

      # Signal
      today_txt <- if (!is.na(d$today_avg)) {
        if (d$today_avg > 0.3) paste0("🟢 Сегодня ", d$today_name, " — исторически +", round(d$today_avg, 2), "% (", d$today_win, "% win)")
        else if (d$today_avg < -0.3) paste0("🔴 Сегодня ", d$today_name, " — исторически ", round(d$today_avg, 2), "% (", d$today_win, "% win)")
        else paste0("⚪ Сегодня ", d$today_name, " — нейтрально (", round(d$today_avg, 2), "%)")
      } else "—"

      streak_txt <- if (d$current_streak > 0)
        paste0("📈 Рост ", d$current_streak, " дн. подряд (макс был ", d$max_up, ")")
        else if (d$current_streak < 0)
        paste0("📉 Падение ", abs(d$current_streak), " дн. подряд (макс был ", d$max_down, ")")
        else "—"

      best_signal <- if (d$best_avg > 0.3)
        paste0("📈 Лучшая покупка: ", d$best_day, " (+", round(d$best_avg, 2), "%, ", d$best_win, "% win)")
        else "Нет явно лучшего дня"
      worst_signal <- if (d$worst_avg < -0.3)
        paste0("📉 Худший день: ", d$worst_day, " (", round(d$worst_avg, 2), "%, ", d$worst_win, "% win)")
        else "Нет явно худшего дня"

      eom_txt <- if (!is.na(d$eom_avg))
        paste0("Конец месяца: ", if (d$eom_avg > 0) "+" else "", round(d$eom_avg, 2),
               "% (", d$eom_win, "% win) vs обычные ", round(d$non_eom_avg, 2), "%")
        else "Нет данных по концу месяца"

      div(style = paste0("border:1px solid ", BORDER, ";border-radius:16px;padding:20px;margin-bottom:16px;background:", CARD, ";"),
        # Header
        div(style = "display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;",
          div(
            tags$span(style = "font-size:1.3rem;font-weight:800;color:#e6edf3;", d$sym),
            tags$span(style = "font-size:0.75rem;color:#555c6b;margin-left:8px;",
              paste0("$", format(round(d$last_price, 4), nsmall = ifelse(d$last_price < 1, 4, 2)),
                     " · ", d$n_days, " дней истории"))),
          div(style = paste0("font-size:0.78rem;font-weight:600;color:",
                             if (d$current_streak > 0) GREEN else if (d$current_streak < 0) RED else GRAY, ";"),
            streak_txt)),

        layout_columns(col_widths = c(7, 5),
          # Left: day-of-week
          div(
            div(style = "font-size:0.82rem;font-weight:600;color:#e6edf3;margin-bottom:8px;",
              "📅 Средний return по дню недели"),
            tagList(dow_bars),
            div(style = "font-size:0.68rem;color:#555c6b;margin-top:6px;",
              paste0("Синяя рамка = сегодня (", d$today_name, ")"))
          ),
          # Right: monthly seasonality + stats
          div(
            div(style = "font-size:0.82rem;font-weight:600;color:#e6edf3;margin-bottom:8px;",
              "🗓 Сезонность по месяцам"),
            div(style = "display:flex;flex-wrap:wrap;gap:2px;margin-bottom:14px;",
              tagList(month_bars)),
            div(style = "font-size:0.68rem;color:#555c6b;margin-bottom:10px;",
              "Синяя линия = текущий месяц"),
            # Stats
            div(style = "font-size:0.78rem;color:#adbac7;line-height:1.6;",
              div(paste0("📊 Серии: средний рост ", d$avg_up, " дн. (макс ", d$max_up,
                         "), среднее падение ", d$avg_down, " дн. (макс ", d$max_down, ")")),
              div(eom_txt))
          )
        ),
        # Signals
        div(style = paste0("margin-top:14px;padding:12px 16px;border-radius:10px;background:",
                           CARD2, ";border:1px solid ", BORDER, ";"),
          div(style = "font-size:0.85rem;font-weight:600;color:#58a6ff;margin-bottom:6px;",
            "🎯 Рекомендации"),
          div(style = "font-size:0.82rem;color:#e6edf3;line-height:1.7;",
            div(style = paste0("color:", if (grepl("🟢", today_txt)) GREEN else if (grepl("🔴", today_txt)) RED else GRAY, ";font-weight:600;"),
              today_txt),
            div(best_signal),
            div(worst_signal))
        )
      )
    })

    # Correlation between the 6 coins
    pw <- price_wide()
    available <- intersect(TOP6_COINS, colnames(pw))
    corr_block <- NULL
    if (length(available) >= 2) {
      rw <- as.data.frame(lapply(pw[, available, drop = FALSE], function(x) c(NA, diff(log(as.numeric(x))))))
      cm <- cor(rw, use = "pairwise.complete.obs")
      corr_rows <- list()
      for (i in seq_len(nrow(cm))) {
        cells <- list()
        for (j in seq_len(ncol(cm))) {
          v <- cm[i, j]
          if (i == j) {
            col <- "#1c2333"; txt <- "—"
          } else {
            txt <- sprintf("%.2f", v)
            col <- if (v > 0.7) "rgba(88,166,255,0.3)" else if (v > 0.4) "rgba(88,166,255,0.15)"
                   else if (v < -0.4) "rgba(248,81,73,0.15)" else "transparent"
          }
          cells[[j]] <- div(style = paste0("padding:6px 10px;text-align:center;font-size:0.72rem;",
                                            "background:", col, ";border-radius:4px;color:#adbac7;"), txt)
        }
        corr_rows[[i]] <- div(style = "display:flex;gap:4px;margin-bottom:4px;align-items:center;",
          div(style = "font-size:0.72rem;color:#e6edf3;width:70px;font-weight:600;", rownames(cm)[i]),
          tagList(cells))
      }
      # Header row
      header_cells <- lapply(colnames(cm), function(x)
        div(style = "font-size:0.65rem;color:#555c6b;width:50px;text-align:center;", substr(gsub("/USD", "", x), 1, 3)))
      corr_block <- div(style = paste0("padding:18px 20px;border-radius:14px;border:1px solid ", BORDER,
                                       ";background:", CARD, ";margin-top:16px;margin-bottom:16px;"),
        div(style = "font-size:0.9rem;font-weight:600;color:#e6edf3;margin-bottom:12px;",
          "🔗 Корреляция между монетами"),
        div(style = "display:flex;gap:4px;margin-bottom:6px;padding-left:74px;", tagList(header_cells)),
        tagList(corr_rows),
        div(style = "font-size:0.72rem;color:#555c6b;margin-top:8px;",
          "Синий = положительная (двигаются вместе), красный = отрицательная. > 0.7 = не открывай одновременно."))
    }

    tagList(
      tags$div(style = "padding:14px 20px;border-radius:12px;border:1px solid #1c2333;background:#0f1419;margin-bottom:18px;",
        tags$span(style = "color:#8b949e;font-size:0.85rem;",
          "Глубокий анализ 6 монет: день недели, сезонность по месяцам, серии роста/падения, ",
          "конец месяца. Синяя рамка = сегодня. Рекомендации — на основе 3 лет истории.")),
      tagList(cards),
      corr_block
    )
  })

}

shinyApp(ui, server)
