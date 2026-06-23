# CryptoScope (Crypto-Analyzer-R)

R/Shiny веб-приложение для анализа финансовых рынков (крипта, акции, форекс): pairs trading, коинтеграция, Z-score сигналы на завтра. Тёмная тема, UI на русском.

## Run & Operate

- Локально (Replit): `bash artifacts/crypto-analyzer/run.sh` — Shiny на порту 3000
- Docker: `docker build -t cryptoscope . && docker run -p 3000:3000 -v cryptoscope-data:/data -e TWELVEDATA_API_KEY=... cryptoscope`
- **Деплой: Railway** (autoscale), порт 3000, persistent volume примонтирован к `/data`
- Обновление данных: cron `0 6 * * *` UTC (09:00 MSK) — `scripts/daily_update.R` докачает последние 5 дней и пересчитает сигналы
- Env: `TWELVEDATA_API_KEY` (нужен для daily update; для запуска UI опционален — работает на запечённых данных), `DB_PATH` (по умолчанию `/data/market.db`), `PORT` (по умолчанию 3000)

## Stack

- **R 4.4** (rocker/r-ver:4.4.0), Shiny + bslib (тёмная тема, Inter)
- dplyr / tidyr / ggplot2 / zoo / DT / RSQLite / jsonlite
- SQLite (`/data/market.db`) — 3 таблицы: `prices`, `signals`, `update_log`
- Twelve Data API (батч ≤8 тикеров, free tier 8 кредитов/мин)
- Контейнер: Docker + cron daemon
- TS-скаффолд (`lib/`, `artifacts/api-server`, `artifacts/mockup-sandbox`, `pnpm-workspace.yaml`) — leftover от Replit-шаблона, к R-приложению НЕ относится

## Where things live

- `artifacts/crypto-analyzer/app.R` — всё приложение (UI + server, один файл)
- `scripts/tickers.R` — 168 тикеров (88 crypto + 50 stocks + 30 forex)
- `scripts/build_db.R` — CSV → SQLite при `docker build`
- `scripts/init_db.R` — первичная загрузка через API (альтернатива)
- `scripts/daily_update.R` — cron: докачка цен + пересчёт сигналов
- `scripts/start.sh` — Docker entrypoint (cron + Shiny)
- `data/all_markets_3yr.csv` — 3 года сырых цен (baked в образ)
- `Dockerfile` — образ + cron job

## Architecture decisions

- **Данные baked в образ + volume поверх**: CSV (3 года, 168 тикеров) копируется в образ и конвертируется в SQLite при `docker build`. На Railway volume монтируется в `/data` — там живёт `market.db`, логи cron и накопленные сигналы. Ежедневный cron обновляет только последние 5 дней.
- **Приоритет источника в UI**: API (только что загружено юзером) → SQLite БД → CSV. Приложение работает офлайн на запечённых данных и с live API одновременно.
- **Twelve Data батчинг**: 8 тикеров/запрос (лимит free tier). Пауза 75с в cron / 8×1с в UI (чтобы не убить Shiny WebSocket idle timeout).
- **Коинтеграция Engle-Granger вручную** (OLS + AR(1) на остатках, без `tseries`/`urca`). t-stat < -2.9 → коинтегрированы. Полупериод = -log(2)/b.
- **AR(1) прогноз Z-score** на следующий день → probability of signal. Backtest: вход |Z|≥2, выход |Z|<0.5, стоп |Z|≥3.5.

## Product

Веб-UI на русском, тёмная тема (GitHub-dark: #0d1117 / #161b22 / #30363d). Три вкладки:
- **📂 Данные** — выбор рынка (crypto/stocks/forex), пресеты тикеров, API-ключ Twelve Data, фильтры по датам/тиикерам, предпросмотр
- **🤝 Pairs Trading** — топ пар, коинтеграция, полупериод, график Z-score спреда, backtest с P&L, AR(1) прогноз на завтра, экспорт CSV
- **🚦 Сигналы** — активные сигналы (Long/Short), сила, Z сейчас / Z прогноз, сводная таблица

## User preferences

- UI на русском
- Тёмная тема GitHub-dark, брендинг «CryptoScope beta» (градиент blue→purple→orange)
- Деплой на Railway с persistent volume для данных

## Gotchas

- **Volume vs build-time DB**: Railway монтирует свежий volume поверх `/data`, скрывая build-time `market.db`. Решено: seed-CSV лежит в `/opt/seed/` (вне volume), а `start.sh` при отсутствии `/data/market.db` пересобирает БД из CSV при первом запуске.
- **API-ключ утёк в git**: захардкожен в `scripts/fetch_local.R`, `fetch_forex.R`, `fetch_remaining.R` (`54ebd565...`). Переработать историю и ротировать ключ.
- **Shiny WebSocket**: `shiny.idle.timeout = 600000` (10 мин). Длинные расчёты (168 тикеров → тысячи пар) могут рвать сессию.
- **Rate limit**: 8 кредитов/мин на free tier. В cron пауза 75с между батчами; в UI 8×1с с `incProgress`.
- **Дублирование логики**: `engle_granger()` и signal-логика живут в двух местах (`app.R` и `scripts/daily_update.R`) — держать синхронно при правках.
- **TS-скаффолд** (`lib/`, `artifacts/api-server`, `mockup-sandbox`, `tsconfig.json`, `pnpm-workspace.yaml`) — не относится к R-приложению, не трогать без необходимости.
- `corrplot` установлен в Dockerfile, но в `app.R` не используется.

## Pointers

- Тикеры: `scripts/tickers.R` (source of truth) + дубликат-presets в `app.R` — синхронизировать
- Схема БД: `scripts/build_db.R` (таблицы prices / signals / update_log)
- Коинтеграция: `engle_granger()` в `app.R` + копия в `scripts/daily_update.R`
- Сигналы: `signals_data()` в `app.R` + `compute_signals_for_market()` в `scripts/daily_update.R`
- Запуск: `artifacts/crypto-analyzer/run.sh` (Replit) / `scripts/start.sh` (Docker/Railway)
