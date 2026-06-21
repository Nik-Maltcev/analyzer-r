library(shiny)
library(bslib)
library(dplyr)
library(tidyr)
library(ggplot2)
library(zoo)
library(DT)
library(tibble)

options(shiny.maxRequestSize = 50 * 1024^2)

dark_theme <- theme_minimal(base_size = 13) +
  theme(
    plot.background  = element_rect(fill = "#161b22", color = NA),
    panel.background = element_rect(fill = "#161b22", color = NA),
    panel.grid.major = element_line(color = "#30363d"),
    panel.grid.minor = element_line(color = "#21262d"),
    axis.text  = element_text(color = "#adbac7"),
    axis.title = element_text(color = "#adbac7"),
    plot.title = element_text(color = "#e6edf3", face = "bold", size = 14),
    plot.subtitle = element_text(color = "#8b949e", size = 11),
    legend.background = element_rect(fill = "#161b22", color = NA),
    legend.text  = element_text(color = "#adbac7"),
    legend.title = element_text(color = "#e6edf3"),
    strip.text   = element_text(color = "#e6edf3", face = "bold"),
    strip.background = element_rect(fill = "#21262d", color = NA)
  )

ORANGE <- "#f7931a"
BLUE   <- "#58a6ff"
GREEN  <- "#3fb950"
RED    <- "#f85149"
GRAY   <- "#8b949e"

ui <- page_navbar(
  title = div(
    style = "display:flex;align-items:center;gap:10px;",
    span(style = "font-size:1.35rem;font-weight:700;color:#f7931a;letter-spacing:-0.5px;", "CryptoAnalyzer"),
    span(style = "font-size:0.78rem;color:#666;margin-top:3px;", "Dependency & Cycle Detector")
  ),
  theme = bs_theme(
    bg            = "#0d1117",
    fg            = "#e6edf3",
    primary       = "#f7931a",
    secondary     = "#30363d",
    base_font     = font_google("Inter"),
    code_font     = font_google("JetBrains Mono"),
    "navbar-bg"          = "#161b22",
    "card-bg"            = "#161b22",
    "card-border-color"  = "#30363d",
    "input-bg"           = "#0d1117",
    "input-border-color" = "#30363d",
    "input-color"        = "#e6edf3",
    "btn-close-color"    = "#e6edf3"
  ),
  fillable = FALSE,

  # ── TAB 1: Upload ──────────────────────────────────────────────────────────
  nav_panel("Загрузка данных", icon = icon("upload"),
    layout_columns(col_widths = c(4, 8),
      card(
        card_header(icon("file-csv"), " Загрузить CSV"),
        card_body(
          fileInput("file", NULL, accept = ".csv",
            buttonLabel = "Выбрать файл",
            placeholder = "coin_id, symbol, date, price…"),
          hr(),
          radioButtons("sep", "Разделитель:",
            choices = c("Запятая (,)" = ",", "Точка с запятой (;)" = ";", "Табуляция" = "\t"),
            selected = ","),
          hr(),
          uiOutput("coin_filter_ui"),
          uiOutput("date_filter_ui"),
          hr(),
          actionButton("analyze", "Анализировать", class = "btn-warning w-100", icon = icon("chart-line"))
        )
      ),
      card(
        card_header(icon("table"), " Предпросмотр"),
        card_body(uiOutput("data_summary"), DTOutput("preview_table"))
      )
    )
  ),

  # ── TAB 2: Correlations ─────────────────────────────────────────────────────
  nav_panel("Корреляции", icon = icon("diagram-project"),
    uiOutput("corr_ui")
  ),

  # ── TAB 3: Rolling corr ─────────────────────────────────────────────────────
  nav_panel("Зависимости во времени", icon = icon("timeline"),
    uiOutput("rolling_corr_ui")
  ),

  # ── TAB 4: Cycles ───────────────────────────────────────────────────────────
  nav_panel("Цикличность", icon = icon("rotate"),
    uiOutput("cycles_ui")
  ),

  # ── TAB 5: Lag analysis ─────────────────────────────────────────────────────
  nav_panel("Лаг-анализ (кто ведёт?)", icon = icon("arrows-left-right"),
    uiOutput("lag_ui")
  )
)

server <- function(input, output, session) {

  # ── Raw data ─────────────────────────────────────────────────────────────────
  raw_data <- reactive({
    req(input$file)
    df <- tryCatch(read.csv(input$file$datapath, sep = input$sep, stringsAsFactors = FALSE),
                   error = function(e) NULL)
    req(!is.null(df))
    need_cols <- c("coin_id", "symbol", "date", "price")
    miss <- setdiff(need_cols, colnames(df))
    if (length(miss) > 0) {
      showNotification(paste("Отсутствуют колонки:", paste(miss, collapse = ", ")), type = "error")
      return(NULL)
    }
    df$date  <- as.Date(df$date)
    df$price <- as.numeric(df$price)
    if ("volume"     %in% colnames(df)) df$volume     <- as.numeric(df$volume)
    if ("market_cap" %in% colnames(df)) df$market_cap <- as.numeric(df$market_cap)
    df <- df[!is.na(df$date) & !is.na(df$price), ]
    df[order(df$coin_id, df$date), ]
  })

  output$coin_filter_ui <- renderUI({
    df <- raw_data(); req(df)
    selectInput("sel_coins", "Монеты:", choices = unique(df$symbol),
                selected = unique(df$symbol), multiple = TRUE, selectize = TRUE)
  })

  output$date_filter_ui <- renderUI({
    df <- raw_data(); req(df)
    dateRangeInput("date_range", "Период:", start = min(df$date), end = max(df$date))
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
      tags$i(class = "fas fa-cloud-upload-alt fa-3x", style = "display:block;margin-bottom:12px;"),
      p("Загрузите CSV с колонками:"),
      tags$code("coin_id, symbol, name, date, price, volume, market_cap")
    ))
    layout_columns(col_widths = c(4,4,4),
      value_box("Монет",    length(unique(df$symbol)),             showcase = icon("coins"),    theme = "warning"),
      value_box("Записей",  format(nrow(df), big.mark = " "),      showcase = icon("database"), theme = "secondary"),
      value_box("Период",   paste(min(df$date), "→", max(df$date)),showcase = icon("calendar"), theme = "secondary")
    )
  })

  output$preview_table <- renderDT({
    df <- raw_data(); req(df)
    datatable(head(df, 200),
              options = list(pageLength = 10, scrollX = TRUE, dom = "tip"),
              style = "bootstrap5", class = "table-dark table-sm")
  })

  # ── Wide-format helpers ───────────────────────────────────────────────────────
  price_wide <- eventReactive(input$analyze, {
    df <- filtered_data(); req(df)
    pw <- df |> select(symbol, date, price) |>
      pivot_wider(names_from = symbol, values_from = price) |>
      arrange(date)
    dates <- pw$date
    mat   <- as.data.frame(pw[, -1])
    rownames(mat) <- as.character(dates)
    mat
  })

  returns_wide <- eventReactive(input$analyze, {
    pw <- price_wide(); req(pw)
    as.data.frame(apply(pw, 2, function(x) {
      xf <- na.approx(x, na.rm = FALSE)
      c(NA, diff(log(xf)))
    }))
  })

  # ── CORRELATIONS ─────────────────────────────────────────────────────────────
  corr_matrix <- reactive({
    cor(price_wide(),   use = "pairwise.complete.obs")
  })
  ret_corr_matrix <- reactive({
    cor(returns_wide(), use = "pairwise.complete.obs")
  })

  output$corr_ui <- renderUI({
    if (!isTruthy(input$analyze)) return(placeholder_msg())
    tagList(
      layout_columns(col_widths = c(6, 6),
        card(card_header("Корреляция цен"),
             card_body(plotOutput("price_corr_plot", height = "420px"))),
        card(card_header("Корреляция логарифмических доходностей"),
             card_body(plotOutput("return_corr_plot", height = "420px")))
      ),
      card(card_header("Топ зависимостей"),
           card_body(DTOutput("top_corr_table")))
    )
  })

  make_heatmap <- function(cm, title) {
    n <- ncol(cm)
    coins <- colnames(cm)
    df <- expand.grid(A = coins, B = coins, stringsAsFactors = FALSE)
    df$val <- as.vector(cm)
    df$A <- factor(df$A, levels = coins)
    df$B <- factor(df$B, levels = rev(coins))
    ggplot(df, aes(A, B, fill = val)) +
      geom_tile(color = "#0d1117", linewidth = 0.5) +
      geom_text(aes(label = round(val, 2)),
                color = ifelse(abs(df$val) > 0.5, "white", "#adbac7"),
                size = max(2.5, 10 / n)) +
      scale_fill_gradient2(low = "#1a3a6b", mid = "#161b22", high = "#f7931a",
                           midpoint = 0, limits = c(-1, 1), name = "Корр.") +
      labs(title = title, x = NULL, y = NULL) +
      dark_theme +
      theme(axis.text.x = element_text(angle = 45, hjust = 1, color = "#adbac7"),
            plot.margin = margin(10, 10, 10, 10))
  }

  output$price_corr_plot  <- renderPlot({ make_heatmap(corr_matrix(),     "Корреляция цен") },
                                         bg = "#161b22")
  output$return_corr_plot <- renderPlot({ make_heatmap(ret_corr_matrix(), "Корреляция доходностей") },
                                         bg = "#161b22")

  output$top_corr_table <- renderDT({
    cm  <- corr_matrix();   req(cm)
    rcm <- ret_corr_matrix()
    pairs <- which(upper.tri(cm), arr.ind = TRUE)
    df <- data.frame(
      "Монета A"            = rownames(cm)[pairs[,1]],
      "Монета B"            = colnames(cm)[pairs[,2]],
      "Корр. цен"           = round(cm[pairs], 3),
      "Корр. доходн."       = round(rcm[pairs], 3),
      stringsAsFactors = FALSE, check.names = FALSE
    )
    df[["Сила"]]       <- ifelse(abs(df[["Корр. цен"]]) > 0.8, "Сильная",
                           ifelse(abs(df[["Корр. цен"]]) > 0.5, "Умеренная", "Слабая"))
    df[["Направление"]] <- ifelse(df[["Корр. цен"]] > 0, "Прямая ↑", "Обратная ↓")
    df <- df[order(-abs(df[["Корр. цен"]])), ]
    datatable(df, options = list(pageLength = 15, dom = "tip"),
              style = "bootstrap5", class = "table-dark table-sm", rownames = FALSE) |>
      formatStyle("Корр. цен",
        color = styleInterval(c(-0.5, 0.5), c(BLUE, "#e6edf3", ORANGE)))
  })

  # ── ROLLING CORRELATION ───────────────────────────────────────────────────────
  output$rolling_corr_ui <- renderUI({
    if (!isTruthy(input$analyze)) return(placeholder_msg())
    tagList(
      card(card_header("Настройки"),
        card_body(layout_columns(col_widths = c(6,6),
          sliderInput("roll_w", "Скользящее окно (дней):", 7, 90, 30, 7),
          uiOutput("pair_sel_ui")
        ))
      ),
      card(card_header("Скользящая корреляция во времени"),
           card_body(plotOutput("roll_corr_plot", height = "380px"))),
      card(card_header("Синхронность движения монет (% движутся в одну сторону)"),
           card_body(plotOutput("sync_plot", height = "340px")))
    )
  })

  output$pair_sel_ui <- renderUI({
    pw <- price_wide(); req(pw)
    coins <- colnames(pw)
    if (length(coins) < 2) return(p("Нужно ≥ 2 монеты"))
    pairs <- combn(coins, 2, simplify = FALSE)
    nms   <- sapply(pairs, paste, collapse = " / ")
    selectInput("sel_pair", "Пара монет:", choices = setNames(seq_along(pairs), nms), selected = 1)
  })

  sel_pair_coins <- reactive({
    pw <- price_wide(); req(pw, input$sel_pair)
    coins <- colnames(pw)
    pairs <- combn(coins, 2, simplify = FALSE)
    pairs[[as.integer(input$sel_pair)]]
  })

  output$roll_corr_plot <- renderPlot({
    pw <- price_wide(); req(pw, input$roll_w)
    pair <- sel_pair_coins(); req(pair)
    dates <- as.Date(rownames(pw))
    x <- pw[[pair[1]]]; y <- pw[[pair[2]]]
    w <- input$roll_w
    rc <- rollapply(data.frame(x = x, y = y), width = w,
                    FUN = function(m) cor(m[,1], m[,2], use = "complete.obs"),
                    by.column = FALSE, align = "right", fill = NA)
    df <- data.frame(date = dates, corr = as.numeric(rc))
    df <- df[!is.na(df$corr), ]
    ggplot(df, aes(date, corr)) +
      geom_hline(yintercept = c(-0.7, 0, 0.7), linetype = c("dashed","solid","dashed"),
                 color = c(RED, GRAY, GREEN), linewidth = 0.6) +
      geom_ribbon(aes(ymin = pmin(corr, 0), ymax = pmax(corr, 0), fill = corr > 0), alpha = 0.25) +
      geom_line(color = ORANGE, linewidth = 0.9) +
      scale_fill_manual(values = c("TRUE" = ORANGE, "FALSE" = BLUE), guide = "none") +
      scale_y_continuous(limits = c(-1.05, 1.05), breaks = seq(-1, 1, 0.25)) +
      labs(title = paste(pair[1], "vs", pair[2], "— скользящая корреляция"),
           subtitle = paste("Окно:", w, "дней  |  зелёная пунктирная линия = ±0.7"),
           x = NULL, y = "Корреляция") +
      dark_theme
  }, bg = "#161b22")

  output$sync_plot <- renderPlot({
    df <- filtered_data(); req(df)
    sync <- df |>
      group_by(symbol) |> arrange(date) |>
      mutate(dir = sign(price - lag(price))) |>
      ungroup() |>
      filter(!is.na(dir)) |>
      group_by(date) |>
      summarise(pct_up   = mean(dir > 0) * 100,
                pct_down = mean(dir < 0) * 100,
                dominant = ifelse(pct_up >= pct_down, "Рост", "Падение"), .groups = "drop")
    ggplot(sync, aes(date, pmax(pct_up, pct_down), fill = dominant)) +
      geom_col(alpha = 0.8, width = 1) +
      scale_fill_manual(values = c("Рост" = GREEN, "Падение" = RED), name = NULL) +
      labs(title = "Синхронность: % монет движутся в одну сторону",
           subtitle = "Высокие столбцы = монеты двигаются вместе",
           x = NULL, y = "% монет") +
      dark_theme + theme(legend.position = "top")
  }, bg = "#161b22")

  # ── CYCLES ────────────────────────────────────────────────────────────────────
  output$cycles_ui <- renderUI({
    if (!isTruthy(input$analyze)) return(placeholder_msg())
    tagList(
      card(card_header("Настройки"),
        card_body(layout_columns(col_widths = c(4,4,4),
          uiOutput("cycle_coin_ui"),
          selectInput("cycle_type", "Тип анализа:",
            choices = c("Автокорреляция (ACF)"    = "acf",
                        "Спектр (Фурье/FFT)"       = "fft",
                        "Сезонная декомпозиция"    = "decomp")),
          sliderInput("max_lag", "Макс. лаг (дней):", 7, 120, 60)
        ))
      ),
      card(card_header("График"), card_body(plotOutput("cycle_plot", height = "420px"))),
      card(card_header("Сводка по всем монетам"), card_body(DTOutput("cycle_summary")))
    )
  })

  output$cycle_coin_ui <- renderUI({
    df <- filtered_data(); req(df)
    selectInput("cycle_coin", "Монета:", choices = unique(df$symbol))
  })

  coin_ts <- reactive({
    df <- filtered_data(); req(df, input$cycle_coin)
    df |> filter(symbol == input$cycle_coin) |> arrange(date) |>
      mutate(log_price = log(price), centered = log_price - mean(log_price, na.rm = TRUE))
  })

  output$cycle_plot <- renderPlot({
    ts_df <- coin_ts(); req(ts_df, nrow(ts_df) > 20, input$cycle_type)
    x <- ts_df$centered

    if (input$cycle_type == "acf") {
      max_lag <- min(input$max_lag, floor(length(x) / 3))
      acf_res <- acf(x, lag.max = max_lag, plot = FALSE, na.action = na.pass)
      ci <- qnorm(0.975) / sqrt(length(x))
      df <- data.frame(lag = as.numeric(acf_res$lag)[-1],
                       acf = as.numeric(acf_res$acf)[-1])
      df$sig <- abs(df$acf) > ci
      ggplot(df, aes(lag, acf, fill = sig)) +
        geom_col(width = 0.8) +
        geom_hline(yintercept = c(-ci, ci), linetype = "dashed", color = GREEN, linewidth = 0.7) +
        geom_hline(yintercept = 0, color = GRAY, linewidth = 0.4) +
        scale_fill_manual(values = c("TRUE" = ORANGE, "FALSE" = "#30363d"), guide = "none") +
        labs(title = paste("ACF —", input$cycle_coin),
             subtitle = paste("Оранжевые столбцы = значимые циклы (CI ±", round(ci, 3), ")"),
             x = "Лаг (дни)", y = "Автокорреляция") +
        dark_theme

    } else if (input$cycle_type == "fft") {
      xc <- x[!is.na(x)]
      n  <- length(xc)
      ft <- fft(xc)
      power   <- Mod(ft[2:(n %/% 2 + 1)])^2
      periods <- n / (1:(n %/% 2))
      df <- data.frame(period = periods, power = power)
      df <- df[df$period >= 2 & df$period <= input$max_lag, ]
      top5 <- head(df[order(-df$power), ], 5)
      ggplot(df, aes(period, power)) +
        geom_line(color = ORANGE, linewidth = 0.8) +
        geom_point(data = top5, aes(period, power), color = "white", size = 3) +
        geom_text(data = top5, aes(label = paste0(round(period,0), " дн.")),
                  color = "white", vjust = -1, size = 3.5) +
        labs(title = paste("Спектр мощности (Фурье) —", input$cycle_coin),
             subtitle = "Пики = доминирующие периоды цикла",
             x = "Период (дней)", y = "Мощность") +
        dark_theme

    } else {
      xf <- na.approx(x, na.rm = FALSE)
      xf[is.na(xf)] <- mean(xf, na.rm = TRUE)
      freq <- 7
      if (length(xf) < freq * 2) {
        return(ggplot() + annotate("text", x = 0.5, y = 0.5,
          label = "Недостаточно данных (нужно > 14 точек)",
          color = "#adbac7", size = 6) + dark_theme)
      }
      ts_obj <- ts(xf, frequency = freq)
      dec <- tryCatch(decompose(ts_obj), error = function(e) NULL)
      req(!is.null(dec))
      dates <- ts_df$date
      long <- bind_rows(
        data.frame(date = dates, value = as.numeric(dec$x),        component = "Данные"),
        data.frame(date = dates, value = as.numeric(dec$trend),    component = "Тренд"),
        data.frame(date = dates, value = as.numeric(dec$seasonal), component = "Сезонность"),
        data.frame(date = dates, value = as.numeric(dec$random),   component = "Остаток")
      )
      long$component <- factor(long$component, levels = c("Данные","Тренд","Сезонность","Остаток"))
      cols <- c("Данные" = "#adbac7", "Тренд" = ORANGE, "Сезонность" = BLUE, "Остаток" = GRAY)
      ggplot(long, aes(date, value, color = component)) +
        geom_line(linewidth = 0.8) +
        facet_wrap(~ component, ncol = 1, scales = "free_y") +
        scale_color_manual(values = cols, guide = "none") +
        labs(title = paste("Декомпозиция (7-дн. период) —", input$cycle_coin),
             x = NULL, y = "log(цена) − среднее") +
        dark_theme
    }
  }, bg = "#161b22")

  output$cycle_summary <- renderDT({
    df <- filtered_data(); req(df)
    coins <- unique(df$symbol)
    res <- lapply(coins, function(coin) {
      s <- df |> filter(symbol == coin) |> arrange(date) |> pull(price)
      lp <- log(s); xc <- lp - mean(lp, na.rm = TRUE)
      if (length(xc) < 20) return(NULL)
      ml <- min(60, floor(length(xc) / 3))
      ar <- tryCatch(acf(xc, lag.max = ml, plot = FALSE, na.action = na.pass), error = function(e) NULL)
      if (is.null(ar)) return(NULL)
      av <- as.numeric(ar$acf)[-1]; lv <- as.numeric(ar$lag)[-1]
      ci <- qnorm(0.975) / sqrt(length(xc))
      sig <- lv[abs(av) > ci]
      peak <- if (length(sig) > 0) sig[which.max(abs(av[abs(av) > ci]))] else NA
      # FFT dominant period
      xf   <- na.approx(xc, na.rm = FALSE); xf[is.na(xf)] <- 0
      n    <- length(xf)
      pw   <- Mod(fft(xf)[2:(n %/% 2 + 1)])^2
      pers <- n / (1:(n %/% 2))
      valid <- pers >= 2 & pers <= 90
      dom_period <- if (any(valid)) round(pers[valid][which.max(pw[valid])]) else NA
      data.frame(
        "Монета"          = coin,
        "Записей"         = length(xc),
        "Осн. цикл ACF"   = ifelse(is.na(peak), "Нет", paste(round(peak), "дн.")),
        "Осн. цикл FFT"   = ifelse(is.na(dom_period), "Нет", paste(dom_period, "дн.")),
        "Значимых лагов"  = length(sig),
        "Цикличность"     = ifelse(length(sig) > 3, "Высокая",
                             ifelse(length(sig) > 0, "Умеренная", "Нет")),
        stringsAsFactors = FALSE, check.names = FALSE
      )
    })
    res <- do.call(rbind, Filter(Negate(is.null), res))
    datatable(res, options = list(pageLength = 20, dom = "tip"),
              style = "bootstrap5", class = "table-dark table-sm", rownames = FALSE)
  })

  # ── LAG ANALYSIS ──────────────────────────────────────────────────────────────
  output$lag_ui <- renderUI({
    if (!isTruthy(input$analyze)) return(placeholder_msg())
    tagList(
      card(card_header("Настройки"),
        card_body(layout_columns(col_widths = c(6,6),
          uiOutput("lag_pair_ui"),
          sliderInput("max_ccf", "Макс. лаг (дней):", 1, 60, 20)
        ))
      ),
      card(card_header("Кросс-корреляция (CCF) — кто движется первым?"),
           card_body(plotOutput("ccf_plot", height = "400px"))),
      card(card_header("Матрица лидерства (оптимальный лаг)"),
        card_body(
          p(style = "color:#8b949e;font-size:0.83rem;",
            "Значение [A,B]: на сколько дней A опережает B (>0) или отстаёт (<0)."),
          DTOutput("lead_lag_table")
        ))
    )
  })

  output$lag_pair_ui <- renderUI({
    pw <- price_wide(); req(pw)
    coins <- colnames(pw)
    if (length(coins) < 2) return(p("Нужно ≥ 2 монеты"))
    pairs <- combn(coins, 2, simplify = FALSE)
    nms   <- sapply(pairs, paste, collapse = " → ")
    selectInput("lag_pair", "Пара A → B:", choices = setNames(seq_along(pairs), nms))
  })

  output$ccf_plot <- renderPlot({
    pw <- price_wide(); req(pw, input$lag_pair, input$max_ccf)
    coins <- colnames(pw)
    pairs <- combn(coins, 2, simplify = FALSE)
    pair  <- pairs[[as.integer(input$lag_pair)]]
    rw    <- returns_wide(); req(rw)
    xr <- rw[[pair[1]]]; yr <- rw[[pair[2]]]
    valid <- !is.na(xr) & !is.na(yr); xr <- xr[valid]; yr <- yr[valid]
    req(length(xr) > 10)
    cr  <- tryCatch(ccf(xr, yr, lag.max = input$max_ccf, plot = FALSE), error = function(e) NULL)
    req(!is.null(cr))
    ci  <- qnorm(0.975) / sqrt(length(xr))
    df  <- data.frame(lag = as.numeric(cr$lag), ccf = as.numeric(cr$acf))
    best_i <- which.max(abs(df$ccf))
    best_lag <- df$lag[best_i]; best_val <- df$ccf[best_i]
    direction <- if (best_lag > 0) paste0(pair[1], " опережает ", pair[2], " на ", best_lag, " дн.")
      else if (best_lag < 0) paste0(pair[2], " опережает ", pair[1], " на ", abs(best_lag), " дн.")
      else "Движутся синхронно"
    df$col <- case_when(
      df$lag == best_lag ~ "white",
      df$lag > 0 ~ ORANGE,
      TRUE ~ BLUE
    )
    df$sig <- abs(df$ccf) > ci
    ggplot(df, aes(lag, ccf, fill = col, alpha = sig)) +
      geom_col(width = 0.8) +
      geom_hline(yintercept = c(-ci, ci), linetype = "dashed", color = GREEN, linewidth = 0.7) +
      geom_hline(yintercept = 0, color = GRAY, linewidth = 0.4) +
      geom_vline(xintercept = best_lag, color = "white", linetype = "dotted", linewidth = 0.8) +
      scale_fill_identity() +
      scale_alpha_manual(values = c("TRUE" = 1, "FALSE" = 0.3), guide = "none") +
      labs(title = paste("CCF:", pair[1], "↔", pair[2]),
           subtitle = direction,
           x = paste0("Лаг (< 0: ", pair[2], " ведёт   > 0: ", pair[1], " ведёт)"),
           y = "Кросс-корреляция") +
      dark_theme
  }, bg = "#161b22")

  output$lead_lag_table <- renderDT({
    pw <- price_wide(); req(pw)
    rw <- returns_wide(); req(rw)
    coins <- colnames(pw)
    if (length(coins) < 2) return(NULL)
    mat <- matrix(NA, length(coins), length(coins), dimnames = list(coins, coins))
    for (i in seq_along(coins)) for (j in seq_along(coins)) {
      if (i == j) next
      xr <- rw[[coins[i]]]; yr <- rw[[coins[j]]]
      v  <- !is.na(xr) & !is.na(yr); xr <- xr[v]; yr <- yr[v]
      if (length(xr) < 10) next
      cr <- tryCatch(ccf(xr, yr, lag.max = 20, plot = FALSE), error = function(e) NULL)
      if (!is.null(cr)) mat[i,j] <- as.numeric(cr$lag)[which.max(abs(as.numeric(cr$acf)))]
    }
    df_out <- as.data.frame(mat)
    datatable(df_out, options = list(pageLength = 20, dom = "t"),
              style = "bootstrap5", class = "table-dark table-sm") |>
      formatStyle(colnames(df_out),
        background = styleInterval(c(-5, 0, 5), c("#1a3a6b","#0d1117","#0d1117","#1a2d1a")),
        color      = styleInterval(c(-0.5, 0.5), c(BLUE, "#e6edf3", ORANGE)))
  })

  # ── Helpers ───────────────────────────────────────────────────────────────────
  placeholder_msg <- function() {
    div(style = "text-align:center;padding:60px;color:#555;",
        icon("chart-simple", style = "font-size:3rem;display:block;margin-bottom:15px;color:#30363d;"),
        p(style = "font-size:1rem;", "Загрузите CSV и нажмите «Анализировать»"))
  }
}

shinyApp(ui = ui, server = server)
