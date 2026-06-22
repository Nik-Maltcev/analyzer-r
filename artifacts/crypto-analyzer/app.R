library(shiny)
library(bslib)
library(dplyr)
library(tidyr)
library(ggplot2)
library(zoo)
library(DT)

options(shiny.maxRequestSize = 50 * 1024^2)

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
    panel.grid.major = element_line(color = BORDER),
    panel.grid.minor = element_blank(),
    axis.text        = element_text(color = "#adbac7"),
    axis.title       = element_text(color = "#adbac7"),
    plot.title       = element_text(color = "#e6edf3", face = "bold", size = 14),
    plot.subtitle    = element_text(color = GRAY, size = 11),
    legend.background = element_rect(fill = CARD, color = NA),
    legend.text      = element_text(color = "#adbac7"),
    legend.title     = element_text(color = "#e6edf3"),
    legend.position  = "top"
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

placeholder_msg <- function(msg = "Загрузите CSV и нажмите «Анализировать»") {
  div(style = "text-align:center;padding:60px 20px;color:#555;",
    tags$i(class = "fas fa-chart-line fa-3x",
           style = "display:block;margin-bottom:14px;color:#30363d;"),
    p(style = "font-size:1.1rem;", msg))
}

badge <- function(txt, col) {
  tags$span(style = paste0(
    "display:inline-block;padding:3px 10px;border-radius:12px;",
    "font-size:0.82rem;font-weight:600;color:#fff;background:", col, ";"),
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
    span(style = "font-size:1.3rem;font-weight:700;color:#58a6ff;", "MarketAnalyzer"),
    span(style = "font-size:0.75rem;color:#555;margin-top:3px;", "простой анализ")
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

  # ── TAB 1: Загрузка ─────────────────────────────────────────────────────
  nav_panel("📂 Данные",
    layout_columns(col_widths = c(4, 8),
      card(
        card_header("Загрузить CSV"),
        card_body(
          fileInput("file", NULL, accept = ".csv",
            buttonLabel = "Выбрать файл",
            placeholder = "ticker, date, close…"),
          p(style = "font-size:0.8rem;color:#8b949e;margin-top:-6px;",
            "Нужны колонки: ticker/symbol + date + close/price. Разделитель определяется автоматически."),
          hr(),
          uiOutput("date_filter_ui"),
          uiOutput("ticker_filter_ui"),
          hr(),
          actionButton("analyze", "Анализировать →",
            class = "btn-primary w-100", icon = icon("play"))
        )
      ),
      card(
        card_header("Предпросмотр данных"),
        card_body(uiOutput("data_summary"), DTOutput("preview_table"))
      )
    )
  ),

  # ── TAB 2: График цен ────────────────────────────────────────────────────
  nav_panel("📈 График цен",
    uiOutput("prices_ui")
  ),

  # ── TAB 3: Связи ─────────────────────────────────────────────────────────
  nav_panel("🔗 Корреляции",
    uiOutput("links_ui")
  ),

  # ── TAB 4: Pairs Trading ─────────────────────────────────────────────────
  nav_panel("🤝 Pairs Trading",
    uiOutput("pairs_ui")
  ),

  # ── TAB 5: Кто ведёт? ────────────────────────────────────────────────────
  nav_panel("🏁 Кто ведёт?",
    uiOutput("leader_ui")
  ),

  # ── TAB 6: Сигналы ──────────────────────────────────────────────────────
  nav_panel("🚦 Сигналы",
    uiOutput("signals_ui")
  )
)

# ── Server ───────────────────────────────────────────────────────────────────
server <- function(input, output, session) {

  # ── Загрузка и нормализация формата ──────────────────────────────────────
  raw_data <- reactive({
    req(input$file)
    first_line <- tryCatch(readLines(input$file$datapath, n = 1, warn = FALSE),
                           error = function(e) "")
    sep_char <- if (grepl("\t", first_line)) "\t" else if (grepl(";", first_line)) ";" else ","
    df <- tryCatch(
      read.csv(input$file$datapath, sep = sep_char, stringsAsFactors = FALSE),
      error = function(e) NULL)
    req(!is.null(df))
    cols <- tolower(colnames(df))
    colnames(df) <- cols

    # Detect ticker column
    ticker_col <- cols[cols %in% c("ticker", "symbol", "coin_id", "name", "id", "asset")][1]
    # Detect date column
    date_col <- cols[cols %in% c("date", "timestamp", "time", "datetime")][1]
    # Detect price column (prefer close)
    price_col <- cols[cols %in% c("close", "price", "close_price", "adj_close", "last", "value")][1]
    # Detect volume column
    vol_col <- cols[cols %in% c("volume", "vol", "volume_24h", "total_volume")][1]

    if (is.na(ticker_col) || is.na(date_col) || is.na(price_col)) {
      showNotification(
        paste0("Не найдены колонки. Нужны: ticker/symbol + date + close/price. ",
               "Найдено: ", paste(colnames(df), collapse = ", ")),
        type = "error", duration = 10)
      return(NULL)
    }

    df$ticker_col <- as.character(df[[ticker_col]])
    df$price_col  <- as.numeric(df[[price_col]])
    df$date       <- as.Date(df[[date_col]])
    if (!is.na(vol_col)) df$volume_col <- as.numeric(df[[vol_col]])

    df <- df[!is.na(df$date) & !is.na(df$price_col) & df$price_col > 0, ]
    df[order(df$ticker_col, df$date), ]
  })

  fmt_label <- reactive({
    df <- raw_data(); req(df)
    "инструментов"
  })

  output$date_filter_ui <- renderUI({
    df <- raw_data(); req(df)
    dateRangeInput("date_range", "Период:", start = min(df$date), end = max(df$date))
  })

  output$ticker_filter_ui <- renderUI({
    df <- raw_data(); req(df)
    tickers <- sort(unique(df$ticker_col))
    lbl <- paste0("Инструменты (", length(tickers), " найдено):")
    selectInput("sel_tickers", lbl,
                choices = tickers, selected = tickers, multiple = TRUE,
                selectize = FALSE, size = min(8, length(tickers)))
  })

  filtered_data <- reactive({
    df <- raw_data(); req(df, input$sel_tickers, input$date_range)
    df <- df[df$ticker_col %in% input$sel_tickers, ]
    df[df$date >= input$date_range[1] & df$date <= input$date_range[2], ]
  })

  output$data_summary <- renderUI({
    df <- raw_data()
    if (is.null(df)) return(div(
      style = "text-align:center;padding:40px;color:#555;",
      tags$i(class = "fas fa-upload fa-3x",
             style = "display:block;margin-bottom:12px;color:#30363d;"),
      p("Загрузите CSV и укажите колонки слева")
    ))
    layout_columns(col_widths = c(4,4,4),
      value_box(fmt_label(), length(unique(df$ticker_col)),
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
    if ("volume_col" %in% colnames(df)) show$volume_col <- df$volume_col
    colnames(show) <- c("Тикер", "Дата", "Цена закрытия",
                        if ("volume_col" %in% colnames(df)) "Объём")
    datatable(head(show, 300),
              options = list(pageLength = 10, scrollX = TRUE, dom = "tip"),
              style = "bootstrap5", class = "table-dark table-sm")
  })

  # ── Вспомогательные реактивы ─────────────────────────────────────────────
  price_wide <- eventReactive(input$analyze, {
    df <- filtered_data(); req(df)
    pw <- df |>
      select(ticker_col, date, price_col) |>
      pivot_wider(names_from = ticker_col, values_from = price_col, values_fn = mean) |>
      arrange(date)
    dates <- pw$date
    mat <- as.data.frame(lapply(pw[, -1, drop = FALSE], as.numeric))
    rownames(mat) <- as.character(dates)
    mat
  })

  returns_wide <- eventReactive(input$analyze, {
    pw <- price_wide(); req(pw)
    as.data.frame(lapply(pw, function(x) {
      xf <- na.approx(as.numeric(x), na.rm = FALSE)
      c(NA, diff(log(xf)))
    }))
  })

  # ── ТАБ: График цен ──────────────────────────────────────────────────────
  output$prices_ui <- renderUI({
    if (!isTruthy(input$analyze)) return(placeholder_msg())
    tagList(
      card(
        card_header("📈 Динамика цен (нормировано к 100 на старте)"),
        card_body(
          p(style = "color:#8b949e;font-size:0.85rem;margin-bottom:8px;",
            "Все инструменты приведены к одной шкале — удобно сравнивать рост."),
          plotOutput("price_chart", height = "420px")
        )
      ),
      uiOutput("price_stats_cards")
    )
  })

  output$price_chart <- renderPlot({
    pw <- price_wide(); req(pw)
    dates <- as.Date(rownames(pw))
    norm <- as.data.frame(lapply(pw, function(x) {
      first_val <- x[!is.na(x)][1]
      if (is.na(first_val) || first_val == 0) return(rep(NA, length(x)))
      x / first_val * 100
    }))
    norm$date <- dates
    long <- pivot_longer(norm, -date, names_to = "Тикер", values_to = "Индекс")
    long <- long[!is.na(long$Индекс), ]
    ggplot(long, aes(date, Индекс, color = Тикер, group = Тикер)) +
      geom_line(linewidth = 0.9, alpha = 0.85) +
      geom_hline(yintercept = 100, color = GRAY, linetype = "dashed", linewidth = 0.5) +
      annotate("text", x = min(dates), y = 102,
               label = "старт (100)", color = GRAY, size = 3.2, hjust = 0) +
      scale_y_continuous(labels = function(x) paste0(x)) +
      labs(x = NULL, y = "Индекс (старт = 100)", color = NULL) +
      dark_theme +
      guides(color = guide_legend(nrow = 2))
  }, bg = CARD)

  output$price_stats_cards <- renderUI({
    pw <- price_wide(); req(pw)
    rows <- lapply(colnames(pw), function(sym) {
      x   <- as.numeric(pw[[sym]]); x <- x[!is.na(x)]
      if (length(x) < 2) return(NULL)
      chg   <- (x[length(x)] / x[1] - 1) * 100
      color <- if (chg >= 0) GREEN else RED
      arrow <- if (chg >= 0) "▲" else "▼"
      tags$div(style = paste0(
        "display:inline-block;margin:6px;padding:12px 18px;",
        "background:", CARD, ";border:1px solid ", BORDER, ";",
        "border-radius:10px;min-width:140px;text-align:center;"),
        tags$div(style = "font-size:0.9rem;color:#adbac7;", sym),
        tags$div(style = paste0("font-size:1.3rem;font-weight:700;color:", color, ";"),
          paste0(arrow, " ", round(chg, 1), "%"))
      )
    })
    div(style = "padding:10px 0;", tagList(rows))
  })

  # ── ТАБ: Корреляции ───────────────────────────────────────────────────────
  output$links_ui <- renderUI({
    if (!isTruthy(input$analyze)) return(placeholder_msg())
    tagList(
      card(
        card_header("🔗 Насколько инструменты движутся вместе"),
        card_body(
          p(style = "color:#8b949e;font-size:0.85rem;",
            "Смотрим на ежедневные изменения цен, а не сами цены. ",
            "Корреляция выше 60% означает сильную синхронность движений."),
          uiOutput("links_cards")
        )
      ),
      card(
        card_header("Все пары"),
        card_body(DTOutput("links_table"))
      )
    )
  })

  corr_pairs <- eventReactive(input$analyze, {
    rw <- returns_wide(); req(rw)
    validate(need(ncol(rw) >= 2, "Нужно минимум 2 инструмента"))
    m  <- as.matrix(rw); storage.mode(m) <- "double"
    cm <- cor(m, use = "pairwise.complete.obs")
    pairs <- which(upper.tri(cm), arr.ind = TRUE)
    df <- data.frame(
      A    = rownames(cm)[pairs[,1]],
      B    = colnames(cm)[pairs[,2]],
      corr = cm[pairs],
      stringsAsFactors = FALSE
    )
    df[order(-abs(df$corr)), ]
  })

  output$links_cards <- renderUI({
    df <- corr_pairs(); req(df)
    top <- head(df, 9)
    rows <- lapply(seq_len(nrow(top)), function(i) {
      r   <- top$corr[i]
      col <- dot_color(r)
      lbl <- corr_label(r)
      pct <- corr_pct(r)
      direction <- if (r > 0) "двигаются в одну сторону" else "двигаются в разные стороны"
      tags$div(style = paste0(
        "border:1px solid ", BORDER, ";border-radius:10px;padding:14px 16px;",
        "margin-bottom:10px;background:", BG, ";"),
        layout_columns(col_widths = c(8, 4),
          div(
            tags$span(style = "font-size:1rem;font-weight:600;color:#e6edf3;",
              top$A[i], " ↔ ", top$B[i]),
            tags$br(),
            tags$span(style = "font-size:0.85rem;color:#8b949e;",
              paste0("Синхронность: ", pct, " — ", direction))
          ),
          div(style = "text-align:right;", badge(lbl, col))
        )
      )
    })
    tagList(
      if (nrow(df) > 9) p(style = "color:#8b949e;font-size:0.82rem;",
        paste0("Топ-9 из ", nrow(df), " пар. Полный список — в таблице ниже.")),
      tagList(rows)
    )
  })

  output$links_table <- renderDT({
    df <- corr_pairs(); req(df)
    out <- data.frame(
      "Тикер A"  = df$A,
      "Тикер B"  = df$B,
      "Характер связи" = sapply(df$corr, corr_label),
      "Синхронность"   = sapply(df$corr, corr_pct),
      "Коэффициент"    = round(df$corr, 3),
      stringsAsFactors = FALSE, check.names = FALSE
    )
    datatable(out, rownames = FALSE,
              options = list(pageLength = 20, dom = "tip", scrollX = TRUE),
              style = "bootstrap5", class = "table-dark table-sm")
  })

  # ── ТАБ: Pairs Trading ────────────────────────────────────────────────────
  output$pairs_ui <- renderUI({
    if (!isTruthy(input$analyze)) return(placeholder_msg())
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

  pairs_coint <- eventReactive(input$analyze, {
    pw <- price_wide(); req(pw)
    validate(need(ncol(pw) >= 2, "Нужно минимум 2 инструмента"))
    tickers <- colnames(pw)
    combos  <- combn(tickers, 2, simplify = FALSE)
    rw <- returns_wide()
    n_combos <- length(combos)

    withProgress(message = "Pairs Trading: расчёт коинтеграции...", value = 0, {
      res <- vector("list", n_combos)
      for (i in seq_along(combos)) {
        p  <- combos[[i]]
        pa <- as.numeric(pw[[p[1]]]); pb <- as.numeric(pw[[p[2]]])
        ra <- as.numeric(rw[[p[1]]]); rb <- as.numeric(rw[[p[2]]])
        # Correlation of returns
        ok_r <- !is.na(ra) & !is.na(rb)
        corr <- if (sum(ok_r) >= 10) cor(ra[ok_r], rb[ok_r]) else NA
        # Cointegration
        cg <- engle_granger(pa, pb)
        res[[i]] <- data.frame(
          A           = p[1],
          B           = p[2],
          corr        = corr,
          halflife    = cg$halflife,
          t_stat      = cg$t_stat,
          is_coint    = cg$is_coint,
          hedge_ratio = cg$hedge_ratio,
          stringsAsFactors = FALSE
        )
        if (i %% 50 == 0 || i == n_combos) {
          incProgress(50 / n_combos,
            detail = paste0(i, " / ", n_combos, " пар"))
        }
      }
    })
    df <- do.call(rbind, Filter(Negate(is.null), res))
    # Score: good pairs have high |corr| AND is_coint AND halflife 5-60
    df$score <- with(df, {
      s <- abs(corr)
      s <- s + ifelse(is_coint, 0.3, 0)
      s <- s + ifelse(!is.na(halflife) & halflife >= 5 & halflife <= 60, 0.3, 0)
      s
    })
    df[order(-df$score), ]
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

  # ── ТАБ: Кто ведёт? ──────────────────────────────────────────────────────
  output$leader_ui <- renderUI({
    if (!isTruthy(input$analyze)) return(placeholder_msg())
    tagList(
      card(
        card_header("🏁 Кто ведёт, а кто следует?"),
        card_body(
          p(style = "color:#8b949e;font-size:0.85rem;",
            "Если один инструмент регулярно меняется раньше другого — он «ведущий». ",
            "Отфильтрованы инструменты с волатильностью < 1%/нед."),
          uiOutput("leader_cards")
        )
      ),
      card(
        card_header("Полная таблица"),
        card_body(DTOutput("leader_table"))
      )
    )
  })

  lead_lag_pairs <- eventReactive(input$analyze, {
    rw <- returns_wide(); req(rw)
    validate(need(ncol(rw) >= 2, "Нужно минимум 2 инструмента"))

    weekly_vol <- sapply(rw, function(x) {
      x <- x[!is.na(x)]
      if (length(x) < 7) return(0)
      sd(x, na.rm = TRUE) * sqrt(7) * 100
    })
    volatile <- names(weekly_vol[weekly_vol >= 1])
    rw <- rw[, volatile, drop = FALSE]
    validate(need(ncol(rw) >= 2, "Недостаточно волатильных инструментов"))

    coins <- colnames(rw)
    pairs <- combn(coins, 2, simplify = FALSE)
    n_pairs <- length(pairs)

    withProgress(message = "Кто ведёт: расчёт лагов...", value = 0, {
      res <- vector("list", n_pairs)
      for (i in seq_along(pairs)) {
        p  <- pairs[[i]]
        xa <- rw[[p[1]]]; xb <- rw[[p[2]]]
        ok <- !is.na(xa) & !is.na(xb)
        if (sum(ok) < 30) { res[[i]] <- NULL; next }
        cc <- tryCatch(ccf(xa[ok], xb[ok], lag.max = 14, plot = FALSE), error = function(e) NULL)
        if (is.null(cc)) { res[[i]] <- NULL; next }
        lags <- as.numeric(cc$lag); acfs <- as.numeric(cc$acf)
        ci   <- qnorm(0.975) / sqrt(sum(ok))
        sig  <- which(abs(acfs) > ci)
        if (length(sig) == 0) {
          best_lag <- 0; strength <- "Нет"
        } else {
          best_idx  <- sig[which.max(abs(acfs[sig]))]
          best_lag  <- lags[best_idx]
          strength  <- if (abs(acfs[best_idx]) > 0.2) "Высокая" else "Низкая"
        }
        leader   <- if (best_lag > 0) p[1] else if (best_lag < 0) p[2] else "Нет"
        follower <- if (best_lag > 0) p[2] else if (best_lag < 0) p[1] else "Нет"
        res[[i]] <- data.frame(A=p[1], B=p[2], lag=best_lag, leader=leader,
                   follower=follower, strength=strength, stringsAsFactors=FALSE)
        if (i %% 50 == 0 || i == n_pairs) {
          incProgress(50 / n_pairs,
            detail = paste0(i, " / ", n_pairs, " пар"))
        }
      }
    })
    do.call(rbind, Filter(Negate(is.null), res))
  })

  output$leader_cards <- renderUI({
    df <- lead_lag_pairs(); req(df)
    df_sig <- df[df$leader != "Нет" & df$strength == "Высокая", ]
    df_sig <- df_sig[order(abs(df_sig$lag)), ]
    top <- head(df_sig, 9)
    if (nrow(top) == 0) {
      return(div(style = "text-align:center;padding:30px;color:#555;",
        p("Явных опережений не обнаружено.")))
    }
    rows <- lapply(seq_len(nrow(top)), function(i) {
      row <- top[i, ]
      days <- abs(row$lag)
      day_word <- if (days == 1) "день" else if (days < 5) "дня" else "дней"
      tags$div(style = paste0(
        "border:1px solid ", BORDER, ";border-radius:10px;padding:14px 16px;",
        "margin-bottom:10px;background:", BG, ";"),
        layout_columns(col_widths = c(8, 4),
          div(
            tags$span(style = "font-size:1rem;font-weight:600;color:#e6edf3;",
              row$leader, " → ", row$follower),
            tags$br(),
            tags$span(style = "font-size:0.85rem;color:#8b949e;",
              paste0(row$leader, " опережает ", row$follower, " на ", days, " ", day_word))
          ),
          div(style = "text-align:right;",
            badge(paste0(days, " ", day_word), ORANGE))
        )
      )
    })
    tagList(
      if (nrow(df_sig) > 9) p(style = "color:#8b949e;font-size:0.82rem;",
        paste0("Топ-9 из ", nrow(df_sig), " значимых пар.")),
      tagList(rows)
    )
  })

  output$leader_table <- renderDT({
    df <- lead_lag_pairs(); req(df)
    out <- data.frame(
      "A"               = df$A,
      "B"               = df$B,
      "Кто опережает"   = ifelse(df$leader == "Нет", "Одновременно", df$leader),
      "На сколько дней" = ifelse(df$lag == 0, "0", paste0(abs(df$lag), " дн.")),
      "Уверенность"     = df$strength,
      stringsAsFactors = FALSE, check.names = FALSE
    )
    datatable(out, rownames = FALSE,
              options = list(pageLength = 20, dom = "tip", scrollX = TRUE),
              style = "bootstrap5", class = "table-dark table-sm")
  })

  # ── ТАБ: Сигналы ──────────────────────────────────────────────────────────
  output$signals_ui <- renderUI({
    if (!isTruthy(input$analyze)) return(placeholder_msg("Загрузите CSV и нажмите «Анализировать»"))
    tagList(
      card(
        card_header("🚦 Торговые сигналы на завтра"),
        card_body(
          p(style = "color:#8b949e;font-size:0.85rem;",
            "Сигналы формируются на основе Z-score спреда коинтегрированных пар. ",
            "Вход при |Z| > 2, выход при |Z| < 0.5. Прогноз — AR(1) модель."),
          uiOutput("signals_active"),
          hr(),
          tags$h6(style = "color:#e6edf3;margin-top:16px;", "📋 Все пары — сводная таблица"),
          DTOutput("signals_table")
        )
      )
    )
  })

  signals_data <- eventReactive(input$analyze, {
    pw <- price_wide(); req(pw)
    pc <- pairs_coint(); req(pc)
    # Only process pairs with strong correlation
    good <- pc[!is.na(pc$corr) & abs(pc$corr) >= 0.7, ]
    if (nrow(good) == 0) return(data.frame())

    n_good <- nrow(good)
    results <- vector("list", n_good)

    withProgress(message = "Сигналы: расчёт прогнозов...", value = 0, {
      for (i in seq_len(n_good)) {
        r  <- good[i, ]
        pa <- as.numeric(pw[[r$A]]); pb <- as.numeric(pw[[r$B]])
        ok <- !is.na(pa) & !is.na(pb) & pa > 0 & pb > 0

        if (sum(ok) < 30) { results[[i]] <- NULL; next }

        hr <- if (!is.na(r$hedge_ratio)) r$hedge_ratio else 1
        spread <- log(pa[ok]) - hr * log(pb[ok])
        mn  <- mean(spread, na.rm = TRUE)
        sd1 <- sd(spread, na.rm = TRUE)
        if (is.na(sd1) || sd1 == 0) { results[[i]] <- NULL; next }

        zscore <- (spread - mn) / sd1
        z_now  <- tail(zscore, 1)

        # AR(1) forecast
        fc <- tryCatch({
          z_clean <- zscore[!is.na(zscore)]
          n_z  <- length(z_clean)
          if (n_z < 20) stop("too few")
          zy   <- z_clean[-1]
          zx   <- z_clean[-n_z]
          arft <- lm(zy ~ zx)
          cf   <- coef(arft)
          z_hat <- cf[1] + cf[2] * tail(z_clean, 1)
          sigma <- sd(residuals(arft), na.rm = TRUE)
          list(z_hat = as.numeric(z_hat), sigma = sigma, ar_b = as.numeric(cf[2]))
        }, error = function(e) NULL)

        z_hat <- if (!is.null(fc)) fc$z_hat else z_now

        # Signal logic
        if (z_now >= 2 || z_hat >= 2) {
          signal <- paste0("Шорт ", r$A, " / Лонг ", r$B)
          signal_type <- "short_a"
        } else if (z_now <= -2 || z_hat <= -2) {
          signal <- paste0("Лонг ", r$A, " / Шорт ", r$B)
          signal_type <- "long_a"
        } else {
          signal <- "Ждать"
          signal_type <- "wait"
        }

        # Strength
        strength <- if (r$is_coint && abs(z_now) >= 2) "Сильный"
                    else if (abs(z_hat) >= 2) "Прогнозный"
                    else if (abs(z_now) >= 1.5) "Формируется"
                    else "Нет"

        results[[i]] <- data.frame(
          A = r$A, B = r$B,
          z_now = round(z_now, 2),
          z_forecast = round(z_hat, 2),
          signal = signal,
          signal_type = signal_type,
          strength = strength,
          is_coint = r$is_coint,
          halflife = r$halflife,
          corr = round(abs(r$corr) * 100),
          stringsAsFactors = FALSE
        )

        if (i %% 20 == 0 || i == n_good) {
          incProgress(20 / n_good, detail = paste0(i, " / ", n_good, " пар"))
        }
      }
    })
    do.call(rbind, Filter(Negate(is.null), results))
  })

  output$signals_active <- renderUI({
    df <- signals_data(); req(df)
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
}

shinyApp(ui, server)
