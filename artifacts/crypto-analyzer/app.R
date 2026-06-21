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

# ── Helpers ─────────────────────────────────────────────────────────────────
corr_label <- function(r) {
  if (is.na(r)) return("—")
  if (r >= 0.7)  return("Движутся вместе")
  if (r >= 0.4)  return("Слабо вместе")
  if (r <= -0.7) return("Движутся противоположно")
  if (r <= -0.4) return("Слабо противоположно")
  return("Независимы")
}
corr_pct <- function(r) {
  if (is.na(r)) return("—")
  paste0(round(abs(r) * 100), "%")
}
arrow_label <- function(lag) {
  if (is.na(lag) || lag == 0) return("Движутся одновременно")
  if (lag > 0) paste0("опережает на ", lag, ifelse(abs(lag) == 1, " день", " дн."))
  else          paste0("отстаёт на ",  abs(lag), ifelse(abs(lag) == 1, " день", " дн."))
}
dot_color <- function(r) {
  if (is.na(r)) return(GRAY)
  if (abs(r) >= 0.7) return(ifelse(r > 0, GREEN, RED))
  if (abs(r) >= 0.4) return(ifelse(r > 0, "#7ee787", "#ff7b72"))
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

ui <- page_navbar(
  title = div(
    style = "display:flex;align-items:center;gap:10px;",
    span(style = "font-size:1.3rem;font-weight:700;color:#f7931a;", "CryptoAnalyzer"),
    span(style = "font-size:0.75rem;color:#555;margin-top:3px;", "простой анализ")
  ),
  theme = bs_theme(
    bg = BG, fg = "#e6edf3", primary = ORANGE, secondary = BORDER,
    base_font = font_google("Inter"),
    "navbar-bg"          = CARD,
    "card-bg"            = CARD,
    "card-border-color"  = BORDER,
    "input-bg"           = BG,
    "input-border-color" = BORDER,
    "input-color"        = "#e6edf3"
  ),
  fillable = FALSE,

  # ── TAB 1: Загрузка ──────────────────────────────────────────────────────
  nav_panel("📂 Данные",
    layout_columns(col_widths = c(4, 8),
      card(
        card_header("Загрузить CSV"),
        card_body(
          fileInput("file", NULL, accept = ".csv",
            buttonLabel = "Выбрать файл",
            placeholder = "coin_id, symbol, date, price…"),
          radioButtons("sep", "Разделитель:",
            choices = c("Запятая (,)" = ",", "Точка с запятой (;)" = ";",
                        "Табуляция" = "\t"),
            selected = ","),
          hr(),
          uiOutput("date_filter_ui"),
          uiOutput("coin_filter_ui"),
          hr(),
          actionButton("analyze", "Анализировать →",
            class = "btn-warning w-100", icon = icon("play"))
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
  nav_panel("🔗 Связи между монетами",
    uiOutput("links_ui")
  ),

  # ── TAB 4: Кто ведёт? ────────────────────────────────────────────────────
  nav_panel("🏁 Кто ведёт рынок?",
    uiOutput("leader_ui")
  )
)

server <- function(input, output, session) {

  # ── Загрузка и очистка данных ─────────────────────────────────────────────
  raw_data <- reactive({
    req(input$file)
    df <- tryCatch(
      read.csv(input$file$datapath, sep = input$sep, stringsAsFactors = FALSE),
      error = function(e) NULL)
    req(!is.null(df))
    need_cols <- c("coin_id", "symbol", "date", "price")
    miss <- setdiff(need_cols, colnames(df))
    if (length(miss) > 0) {
      showNotification(paste("Отсутствуют колонки:", paste(miss, collapse = ", ")),
                       type = "error"); return(NULL)
    }
    df$date  <- as.Date(df$date)
    df$price <- as.numeric(df$price)
    if ("volume"     %in% colnames(df)) df$volume     <- as.numeric(df$volume)
    if ("market_cap" %in% colnames(df)) df$market_cap <- as.numeric(df$market_cap)
    df <- df[!is.na(df$date) & !is.na(df$price) & df$price > 0, ]
    df[order(df$coin_id, df$date), ]
  })

  output$date_filter_ui <- renderUI({
    df <- raw_data(); req(df)
    dateRangeInput("date_range", "Период:", start = min(df$date), end = max(df$date))
  })

  output$coin_filter_ui <- renderUI({
    df <- raw_data(); req(df)
    coins <- unique(df$symbol)
    selectInput("sel_coins", paste0("Монеты (", length(coins), " найдено):"),
                choices = coins, selected = coins, multiple = TRUE,
                selectize = FALSE, size = min(8, length(coins)))
  })

  filtered_data <- reactive({
    df <- raw_data(); req(df, input$sel_coins, input$date_range)
    df <- df[df$symbol %in% input$sel_coins, ]
    df[df$date >= input$date_range[1] & df$date <= input$date_range[2], ]
  })

  output$data_summary <- renderUI({
    df <- raw_data()
    if (is.null(df)) return(div(
      style = "text-align:center;padding:40px;color:#555;",
      tags$i(class = "fas fa-upload fa-3x",
             style = "display:block;margin-bottom:12px;color:#30363d;"),
      p("Нужны колонки:"),
      tags$code("coin_id, symbol, date, price")
    ))
    layout_columns(col_widths = c(4,4,4),
      value_box("Монет",   length(unique(df$symbol)),
                showcase = icon("coins"),    theme = "warning"),
      value_box("Записей", format(nrow(df), big.mark = " "),
                showcase = icon("database"), theme = "secondary"),
      value_box("Период",  paste(format(min(df$date), "%d.%m.%y"),
                                 "–", format(max(df$date), "%d.%m.%y")),
                showcase = icon("calendar"), theme = "secondary")
    )
  })

  output$preview_table <- renderDT({
    df <- raw_data(); req(df)
    show_cols <- intersect(c("symbol","date","price","volume","market_cap"), colnames(df))
    datatable(head(df[, show_cols], 300),
              options = list(pageLength = 10, scrollX = TRUE, dom = "tip"),
              style = "bootstrap5", class = "table-dark table-sm")
  })

  # ── Вспомогательные реактивы ──────────────────────────────────────────────
  price_wide <- eventReactive(input$analyze, {
    df <- filtered_data(); req(df)
    pw <- df |>
      select(symbol, date, price) |>
      pivot_wider(names_from = symbol, values_from = price, values_fn = mean) |>
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

  # ── ТАБ: График цен ───────────────────────────────────────────────────────
  output$prices_ui <- renderUI({
    if (!isTruthy(input$analyze)) return(placeholder_msg())
    tagList(
      card(
        card_header("📈 Динамика цен (нормировано к 100 на старте)"),
        card_body(
          p(style = "color:#8b949e;font-size:0.85rem;margin-bottom:8px;",
            "Все монеты приведены к одной шкале — удобно сравнивать рост."),
          plotOutput("price_chart", height = "420px")
        )
      ),
      uiOutput("price_stats_cards")
    )
  })

  output$price_chart <- renderPlot({
    pw <- price_wide(); req(pw)
    dates <- as.Date(rownames(pw))
    # Normalize to 100 at first non-NA value
    norm <- as.data.frame(lapply(pw, function(x) {
      first_val <- x[!is.na(x)][1]
      if (is.na(first_val) || first_val == 0) return(rep(NA, length(x)))
      x / first_val * 100
    }))
    norm$date <- dates
    long <- pivot_longer(norm, -date, names_to = "Монета", values_to = "Индекс")
    long <- long[!is.na(long$Индекс), ]
    n_coins <- length(unique(long$Монета))
    ggplot(long, aes(date, Индекс, color = Монета, group = Монета)) +
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
    coins <- colnames(pw)
    rows <- lapply(coins, function(sym) {
      x     <- as.numeric(pw[[sym]])
      x     <- x[!is.na(x)]
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

  # ── ТАБ: Связи ────────────────────────────────────────────────────────────
  output$links_ui <- renderUI({
    if (!isTruthy(input$analyze)) return(placeholder_msg())
    tagList(
      card(
        card_header("🔗 Как монеты связаны между собой"),
        card_body(
          p(style = "color:#8b949e;font-size:0.85rem;",
            "Смотрим на доходности (ежедневные изменения цен), а не на сами цены. ",
            "Это показывает реальную связь движений."),
          uiOutput("links_cards")
        )
      ),
      card(
        card_header("Таблица всех пар"),
        card_body(DTOutput("links_table"))
      )
    )
  })

  corr_pairs <- eventReactive(input$analyze, {
    rw <- returns_wide(); req(rw)
    validate(need(ncol(rw) >= 2, "Нужно минимум 2 монеты"))
    coins <- colnames(rw)
    m <- as.matrix(rw); storage.mode(m) <- "double"
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
    top <- head(df, 9)  # show top 9 strongest relationships
    rows <- lapply(seq_len(nrow(top)), function(i) {
      r   <- top$corr[i]
      col <- dot_color(r)
      lbl <- corr_label(r)
      pct <- corr_pct(r)
      direction <- if (r > 0) "одновременно растут и падают" else "двигаются в разные стороны"
      tags$div(style = paste0(
        "border:1px solid ", BORDER, ";border-radius:10px;padding:14px 16px;",
        "margin-bottom:10px;background:", BG, ";"),
        layout_columns(col_widths = c(8, 4),
          div(
            tags$span(style = paste0("font-size:1rem;font-weight:600;color:#e6edf3;"),
              top$A[i], " ↔ ", top$B[i]),
            tags$br(),
            tags$span(style = "font-size:0.85rem;color:#8b949e;",
              paste0("Они ", direction, " в ", pct, " случаев"))
          ),
          div(style = "text-align:right;",
            badge(lbl, col)
          )
        )
      )
    })
    tagList(
      if (nrow(df) > 9) p(style = "color:#8b949e;font-size:0.82rem;",
        paste0("Показаны топ-9 из ", nrow(df), " пар. Полный список — в таблице ниже.")),
      tagList(rows)
    )
  })

  output$links_table <- renderDT({
    df <- corr_pairs(); req(df)
    out <- data.frame(
      "Монета A"  = df$A,
      "Монета B"  = df$B,
      "Связь"     = sapply(df$corr, corr_label),
      "Совпадений" = sapply(df$corr, corr_pct),
      "Сила"      = round(abs(df$corr), 2),
      stringsAsFactors = FALSE, check.names = FALSE
    )
    datatable(out, rownames = FALSE,
              options = list(pageLength = 20, dom = "tip", scrollX = TRUE),
              style = "bootstrap5", class = "table-dark table-sm")
  })

  # ── ТАБ: Кто ведёт? ───────────────────────────────────────────────────────
  output$leader_ui <- renderUI({
    if (!isTruthy(input$analyze)) return(placeholder_msg())
    tagList(
      card(
        card_header("🏁 Кто ведёт, а кто следует?"),
        card_body(
          p(style = "color:#8b949e;font-size:0.85rem;",
            "Если одна монета регулярно меняется раньше другой — значит, она «ведущая». ",
            "Это можно использовать для понимания рыночных сигналов."),
          uiOutput("leader_cards")
        )
      ),
      card(
        card_header("Полная таблица опережений"),
        card_body(DTOutput("leader_table"))
      )
    )
  })

  lead_lag_pairs <- eventReactive(input$analyze, {
    rw <- returns_wide(); req(rw)
    validate(need(ncol(rw) >= 2, "Нужно минимум 2 монеты"))
    coins <- colnames(rw)
    pairs <- combn(coins, 2, simplify = FALSE)
    res <- lapply(pairs, function(p) {
      xa <- rw[[p[1]]]; xb <- rw[[p[2]]]
      ok <- !is.na(xa) & !is.na(xb)
      if (sum(ok) < 30) return(NULL)
      cc <- tryCatch(ccf(xa[ok], xb[ok], lag.max = 14, plot = FALSE), error = function(e) NULL)
      if (is.null(cc)) return(NULL)
      lags <- as.numeric(cc$lag); acfs <- as.numeric(cc$acf)
      ci   <- qnorm(0.975) / sqrt(sum(ok))
      sig  <- which(abs(acfs) > ci)
      if (length(sig) == 0) {
        best_lag <- 0; strength <- "Нет"
      } else {
        best_idx  <- sig[which.max(abs(acfs[sig]))]
        best_lag  <- lags[best_idx]
        best_corr <- abs(acfs[best_idx])
        strength  <- if (best_corr > 0.2) "Высокая" else "Низкая"
      }
      leader <- if (best_lag > 0) p[1] else if (best_lag < 0) p[2] else "Нет"
      follower <- if (best_lag > 0) p[2] else if (best_lag < 0) p[1] else "Нет"
      data.frame(
        A = p[1], B = p[2],
        lag       = best_lag,
        leader    = leader,
        follower  = follower,
        strength  = strength,
        stringsAsFactors = FALSE
      )
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
        p("Явных опережений не обнаружено. Попробуйте выбрать больший диапазон дат.")))
    }
    rows <- lapply(seq_len(nrow(top)), function(i) {
      row <- top[i, ]
      days <- abs(row$lag)
      day_word <- if (days == 1) "день" else if (days < 5) "дня" else "дней"
      desc <- paste0(row$leader, " опережает ", row$follower, " на ",
                     days, " ", day_word)
      tags$div(style = paste0(
        "border:1px solid ", BORDER, ";border-radius:10px;padding:14px 16px;",
        "margin-bottom:10px;background:", BG, ";"),
        layout_columns(col_widths = c(8, 4),
          div(
            tags$span(style = "font-size:1rem;font-weight:600;color:#e6edf3;",
              row$leader, " → ", row$follower),
            tags$br(),
            tags$span(style = "font-size:0.85rem;color:#8b949e;", desc)
          ),
          div(style = "text-align:right;",
            badge(paste0(days, " ", day_word), ORANGE)
          )
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
      "Монета A" = df$A,
      "Монета B" = df$B,
      "Кто опережает"  = ifelse(df$leader == "Нет", "Одновременно", df$leader),
      "На сколько дней" = ifelse(df$lag == 0, "0", paste0(abs(df$lag), " дн.")),
      "Уверенность"    = df$strength,
      stringsAsFactors = FALSE, check.names = FALSE
    )
    datatable(out, rownames = FALSE,
              options = list(pageLength = 20, dom = "tip", scrollX = TRUE),
              style = "bootstrap5", class = "table-dark table-sm")
  })
}

shinyApp(ui, server)
