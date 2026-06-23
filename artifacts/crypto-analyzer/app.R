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

# Keep WebSocket alive during long operations
options(shiny.idle.timeout = 600000)  # 10 min

ORANGE <- "#f7931a"
BLUE   <- "#58a6ff"
GREEN  <- "#3fb950"
RED    <- "#f85149"
GRAY   <- "#8b949e"
BG     <- "#0d1117"
CARD   <- "#161b22"
BORDER <- "#30363d"

dark_theme <- theme_minimal(base_size = 13) +
  theme(
    plot.background  = element_rect(fill = CARD, color = NA),
    panel.background = element_rect(fill = CARD, color = NA),
    panel.grid.major = element_line(color = "#1c2128", linewidth = 0.4),
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
    style = "display:flex;align-items:center;gap:10px;",
    tags$span(style = "
      font-size:1.4rem;font-weight:800;
      background:linear-gradient(135deg, #58a6ff 0%, #a78bfa 50%, #f7931a 100%);
      -webkit-background-clip:text;-webkit-text-fill-color:transparent;
      background-clip:text;", "CryptoScope"),
    tags$span(style = "
      font-size:0.7rem;color:#555;margin-top:4px;
      padding:2px 8px;border:1px solid #30363d;border-radius:20px;", "beta")
  ),
  theme = bs_theme(
    bg = BG, fg = "#e6edf3", primary = BLUE, secondary = BORDER,
    base_font = font_google("Inter"),
    "navbar-bg"          = CARD,
    "card-bg"            = CARD,
    "card-border-color"  = BORDER,
    "input-bg"           = BG,
    "input-border-color" = BORDER,
    "input-color"        = "#e6edf3"
  ),
  fillable = FALSE,
  header = tags$head(tags$style(HTML("
    /* Global polish */
    body { letter-spacing: -0.01em; }
    .navbar { border-bottom: 1px solid #21262d; backdrop-filter: blur(12px); }
    .nav-link { font-weight: 500; font-size: 0.9rem; transition: all 0.2s ease; }
    .nav-link:hover { color: #58a6ff !important; transform: translateY(-1px); }
    .nav-link.active { color: #58a6ff !important; border-bottom: 2px solid #58a6ff; }

    /* Cards */
    .card { border-radius: 14px; border: 1px solid #21262d; transition: border-color 0.2s ease; }
    .card:hover { border-color: #30363d; }
    .card-header { font-weight: 600; font-size: 0.95rem; border-bottom: 1px solid #21262d; padding: 14px 18px; }

    /* Buttons */
    .btn-primary {
      background: linear-gradient(135deg, #58a6ff 0%, #388bfd 100%);
      border: none; border-radius: 10px; font-weight: 600;
      padding: 10px 20px; transition: all 0.2s ease;
      box-shadow: 0 4px 14px rgba(88,166,255,0.25);
    }
    .btn-primary:hover {
      transform: translateY(-1px);
      box-shadow: 0 6px 20px rgba(88,166,255,0.4);
    }

    /* File input */
    .form-control { border-radius: 8px; }

    /* Value boxes */
    .bslib-value-box { border-radius: 12px; border: 1px solid #21262d; }

    /* Tables */
    .dataTables_wrapper { font-size: 0.88rem; }
    table.dataTable { border-collapse: separate; border-spacing: 0; }
    table.dataTable thead th { border-bottom: 2px solid #30363d; font-weight: 600; }
    table.dataTable tbody tr { transition: background 0.15s ease; }
    table.dataTable tbody tr:hover { background: #1c2128 !important; }

    /* Select inputs */
    select.form-select, select.form-control {
      border-radius: 8px; background-color: #0d1117;
      border: 1px solid #30363d; color: #e6edf3;
    }

    /* Progress bar */
    .shiny-notification { background: #161b22; border: 1px solid #30363d; border-radius: 10px; color: #e6edf3; }

    /* Scrollbar */
    ::-webkit-scrollbar { width: 8px; height: 8px; }
    ::-webkit-scrollbar-track { background: #0d1117; }
    ::-webkit-scrollbar-thumb { background: #30363d; border-radius: 4px; }
    ::-webkit-scrollbar-thumb:hover { background: #484f58; }

    /* Checkbox */
    .form-check-input:checked { background-color: #58a6ff; border-color: #58a6ff; }
  "))),

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
    uiOutput("signals_ui")
  ),

  # ── TAB 4: Понятные сигналы ─────────────────────────────────────────────
  nav_panel("💡 Понятные сигналы",
    # Calculator settings (static UI, so inputs persist)
    div(style = "padding:16px 20px;border-radius:12px;border:1px solid #30363d;background:#161b22;margin-bottom:18px;",
      div(style = "font-size:0.9rem;font-weight:600;color:#e6edf3;margin-bottom:12px;",
        "Настройки калькулятора (применяются ко всем сигналам)"),
      layout_columns(col_widths = c(3, 3, 3, 3),
        div(
          tags$label(style = "font-size:0.78rem;color:#8b949e;", "Капитал на сделку (USDT)"),
          numericInput("calc_capital", NULL, value = 100, min = 10, max = 100000, step = 10, width = "100%")
        ),
        div(
          tags$label(style = "font-size:0.78rem;color:#8b949e;", "Плечо"),
          sliderInput("calc_leverage", NULL, min = 1, max = 20, value = 1, step = 1, width = "100%", post = "x")
        ),
        div(
          tags$label(style = "font-size:0.78rem;color:#8b949e;", "Комиссия taker (% / сторону)"),
          numericInput("calc_taker", NULL, value = 0.02, min = 0, max = 1, step = 0.01, width = "100%")
        ),
        div(
          tags$label(style = "font-size:0.78rem;color:#8b949e;", "Финансирование (% / 8ч)"),
          numericInput("calc_funding", NULL, value = 0.01, min = 0, max = 0.1, step = 0.005, width = "100%")
        )
      ),
      div(style = "font-size:0.72rem;color:#555;margin-top:8px;",
        "MEXC Perpetual Futures: taker 0.02%, maker 0.00%, финансирование ~0.01% каждые 8 часов. ",
        "Комиссии: 4 заполнения (2 ноги × вход + выход). ",
        "Финансирование: worst-case (обе ноги платят). Измените под свой аккаунт.")
    ),
    uiOutput("clear_signals_ui")
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
    good <- df[!is.na(df$corr) & abs(df$corr) >= 0.7, , drop = FALSE]
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
    active <- df[df$signal_type != "wait", ]
    active <- active[order(-abs(active$z_now)), ]

    if (nrow(active) == 0) {
      return(div(style = "text-align:center;padding:30px;color:#8b949e;",
        tags$i(class = "fas fa-check-circle fa-2x",
               style = "display:block;margin-bottom:10px;color:#3fb950;"),
        p("Нет активных сигналов. Все пары в нейтральной зоне.")))
    }

    top <- head(active, 12)
    rows <- lapply(seq_len(nrow(top)), function(i) {
      r <- top[i, ]
      is_short <- r$signal_type == "short_a"
      sig_col  <- if (is_short) RED else GREEN
      sig_icon <- if (is_short) "📉" else "📈"
      str_col  <- switch(r$strength,
        "Сильный"     = GREEN,
        "Прогнозный"  = ORANGE,
        "Формируется" = BLUE,
        GRAY)

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
        )
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

    # ── Calculator inputs (with fallbacks for first render) ─────────────────
    cap       <- if (isTruthy(input$calc_capital))  input$calc_capital  else 100
    lev       <- if (isTruthy(input$calc_leverage)) input$calc_leverage else 1
    taker_fee <- if (isTruthy(input$calc_taker))    input$calc_taker    else 0.02
    fund_rate <- if (isTruthy(input$calc_funding))  input$calc_funding  else 0.01
    pos_size  <- cap * lev              # total position across both legs
    leg_size  <- pos_size / 2           # per-leg position

    cards <- lapply(items, function(s) {
      is_short  <- s$signal_type == "short_a"
      sig_col   <- if (is_short) RED else GREEN
      sig_icon  <- if (is_short) "📉" else "📈"

      # Hold time estimate
      hold_days <- if (!is.na(s$halflife) && s$halflife > 0) s$halflife
                   else if (!is.null(s$bt) && s$bt$has_history && !is.na(s$bt$avg_hold)) s$bt$avg_hold
                   else 10
      hold_txt <- if (!is.na(s$halflife) && s$halflife > 0)
        paste0("~", s$halflife, " дн. (полупериод)")
        else if (!is.null(s$bt) && s$bt$has_history && !is.na(s$bt$avg_hold))
          paste0("~", s$bt$avg_hold, " дн. (по истории)")
          else "~10 дн. (по умолчанию)"

      # ── Profit/Loss estimates (% of position) ─────────────────────────────
      z_abs <- abs(s$z_now)
      if (!is.null(s$bt) && s$bt$has_history && !is.na(s$bt$avg_win)) {
        tp_pct <- s$bt$avg_win
        sl_pct <- if (!is.na(s$bt$avg_loss)) abs(s$bt$avg_loss) else 0
        src_txt <- paste0("по истории (", s$bt$n_trades, " сделок)")
      } else {
        # Theoretical: movement from current Z to TP (0.5) or SL (3.5)
        sd_pct <- s$bt$sd_spread_pct
        tp_pct <- round((z_abs - 0.5) * sd_pct, 2)
        sl_pct <- round((3.5 - z_abs) * sd_pct, 2)
        src_txt <- "теоретический расчёт"
      }
      if (is.na(tp_pct) || tp_pct <= 0) tp_pct <- 0.1
      if (is.na(sl_pct) || sl_pct <= 0) sl_pct <- 0.1

      # ── Costs (in USDT) ───────────────────────────────────────────────────
      # Commission: 2 legs × (entry + exit) = 4 fills, taker fee per side
      comm <- round(4 * leg_size * taker_fee / 100, 2)
      # Funding: position × rate × periods (3 per day), worst-case both legs pay
      fund_periods <- hold_days * 3
      funding <- round(pos_size * fund_rate / 100 * fund_periods, 2)

      # ── Net P&L (USDT) ────────────────────────────────────────────────────
      gross_tp <- round(pos_size * tp_pct / 100, 2)
      gross_sl <- round(pos_size * sl_pct / 100, 2)
      net_tp   <- round(gross_tp - comm - funding, 2)
      net_sl   <- round(-(gross_sl + comm + funding), 2)
      rr_ratio <- if (net_sl != 0) round(abs(net_tp / net_sl), 2) else NA

      tp_col <- if (net_tp > 0) GREEN else RED
      sl_col <- RED

      # Strength
      str_col <- switch(s$strength,
        "Сильный"     = GREEN,
        "Прогнозный"  = ORANGE,
        "Формируется" = BLUE,
        GRAY)

      div(style = paste0(
        "border:2px solid ", sig_col, ";border-radius:14px;padding:18px 20px;",
        "margin-bottom:16px;background:", BG, ";box-shadow:0 0 20px ", sig_col, "22;"),
        # Header: action
        div(style = paste0("font-size:1.15rem;font-weight:700;color:", sig_col, ";margin-bottom:14px;"),
          sig_icon, " ", s$signal),
        # Grid: 4 key facts
        layout_columns(col_widths = c(3, 3, 3, 3),
          div(style = paste0("padding:12px;border-radius:8px;background:", CARD, ";border:1px solid ", BORDER, ";"),
            div(style = "font-size:0.75rem;color:#8b949e;margin-bottom:4px;", "Когда входить"),
            div(style = "font-size:0.95rem;font-weight:600;color:#e6edf3;", "Сейчас"),
            div(style = "font-size:0.78rem;color:#555;", paste0("Z = ", s$z_now))
          ),
          div(style = paste0("padding:12px;border-radius:8px;background:", CARD, ";border:1px solid ", BORDER, ";"),
            div(style = "font-size:0.75rem;color:#8b949e;margin-bottom:4px;", "Когда выходить"),
            div(style = "font-size:0.85rem;font-weight:600;color:#e6edf3;", "TP: Z → ±0.5"),
            div(style = "font-size:0.78rem;color:#555;", paste0("SL: |Z| → 3.5"))
          ),
          div(style = paste0("padding:12px;border-radius:8px;background:", CARD, ";border:1px solid ", BORDER, ";"),
            div(style = "font-size:0.75rem;color:#8b949e;margin-bottom:4px;", "Сколько держать"),
            div(style = "font-size:0.95rem;font-weight:600;color:#e6edf3;", hold_txt)
          ),
          div(style = paste0("padding:12px;border-radius:8px;background:", CARD, ";border:1px solid ", BORDER, ";"),
            div(style = "font-size:0.75rem;color:#8b949e;margin-bottom:4px;", "Сила сигнала"),
            div(style = paste0("font-size:0.95rem;font-weight:600;color:", str_col, ";"), s$strength),
            div(style = "font-size:0.72rem;color:#555;", src_txt)
          )
        ),
        # ── Calculator block ────────────────────────────────────────────────
        div(style = paste0("margin-top:14px;padding:14px 16px;border-radius:10px;",
                           "background:", CARD, ";border:1px solid ", BORDER, ";"),
          div(style = "font-size:0.85rem;font-weight:600;color:#e6edf3;margin-bottom:10px;",
            "🧮 Калькулятор прибыли (MEXC Perpetual)"),
          layout_columns(col_widths = c(3, 3, 3, 3),
            # Position size
            div(
              div(style = "font-size:0.72rem;color:#8b949e;", "Размер позиции"),
              div(style = "font-size:0.95rem;font-weight:600;color:#e6edf3;",
                paste0("$", format(pos_size, big.mark = " ", scientific = FALSE))),
              div(style = "font-size:0.68rem;color:#555;",
                paste0("$", cap, " × ", lev, "x"))
            ),
            # Commission
            div(
              div(style = "font-size:0.72rem;color:#8b949e;", "Комиссии (вход+выход)"),
              div(style = "font-size:0.95rem;font-weight:600;color:#f85149;",
                paste0("-$", comm)),
              div(style = "font-size:0.68rem;color:#555;",
                paste0("4 × $", format(leg_size, big.mark = " "), " × ", taker_fee, "%"))
            ),
            # Funding
            div(
              div(style = "font-size:0.72rem;color:#8b949e;", paste0("Финансирование (", fund_periods, " раз)")),
              div(style = "font-size:0.95rem;font-weight:600;color:#f85149;",
                paste0("-$", funding)),
              div(style = "font-size:0.68rem;color:#555;",
                paste0(fund_rate, "% / 8ч × ", hold_days, " дн."))
            ),
            # R/R ratio
            div(
              div(style = "font-size:0.72rem;color:#8b949e;", "Risk / Reward"),
              div(style = paste0("font-size:1.1rem;font-weight:700;color:",
                                 if (!is.na(rr_ratio) && rr_ratio >= 1.5) GREEN else ORANGE, ";"),
                if (!is.na(rr_ratio)) paste0("1:", round(rr_ratio, 1)) else "—"),
              div(style = "font-size:0.68rem;color:#555;", "профит / убыток")
            )
          ),
          # Net P&L
          div(style = "border-top:1px solid #30363d;margin:10px 0;"),
          layout_columns(col_widths = c(6, 6),
            div(style = paste0("text-align:center;padding:10px;border-radius:8px;",
                               "background:#0f2a1a;border:1px solid ", GREEN, ";"),
              div(style = "font-size:0.75rem;color:#8b949e;", "Чистая прибыль (TP)"),
              div(style = paste0("font-size:1.3rem;font-weight:700;color:", tp_col, ";"),
                paste0(if (net_tp > 0) "+" else "", "$", net_tp)),
              div(style = "font-size:0.68rem;color:#555;",
                paste0("+", gross_tp, " − ", comm, " − ", funding))
            ),
            div(style = paste0("text-align:center;padding:10px;border-radius:8px;",
                               "background:#2a0f0f;border:1px solid ", RED, ";"),
              div(style = "font-size:0.75rem;color:#8b949e;", "Чистый убыток (SL)"),
              div(style = paste0("font-size:1.3rem;font-weight:700;color:", sl_col, ";"),
                paste0("$", net_sl)),
              div(style = "font-size:0.68rem;color:#555;",
                paste0("-(", gross_sl, " + ", comm, " + ", funding, ")"))
            )
          )
        ),
        # Footer: meta
        div(style = "margin-top:12px;font-size:0.8rem;color:#8b949e;",
          badge(s$strength, str_col), "  ",
          if (s$is_coint) "✅ Коинтегрированы" else "⚠️ Не коинтегрированы",
          "  ·  Корреляция: ", s$corr, "%",
          if (!is.na(s$halflife)) paste0("  ·  Полупериод: ", s$halflife, " дн.") else "")
      )
    })

    tagList(
      # ── Info banner ───────────────────────────────────────────────────────
      tags$div(style = "padding:12px 18px;border-radius:10px;border:1px solid #30363d;background:#0d1117;margin-bottom:18px;",
        tags$span(style = "color:#8b949e;font-size:0.85rem;",
          "Сигналы на основе Z-score спреда коинтегрированных пар. ",
          "Вход при |Z| ≥ 2, TP при |Z| < 0.5, SL при |Z| ≥ 3.5. ",
          "Профит — по истории backtest'а пары (или теоретический, если истории нет).")),
      # ── Signal cards ──────────────────────────────────────────────────────
      tagList(cards)
    )
  })

}

shinyApp(ui, server)
