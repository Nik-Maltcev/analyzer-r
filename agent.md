# MEANX (Crypto-Analyzer-R)

Python/FastAPI веб-приложение для анализа финансовых рынков (крипта, акции, форекс): pairs trading, коинтеграция, Z-score сигналы. Тёмная тема, UI на русском (+ региональные варианты pt-BR / id).

## Run & Operate

- Docker: `docker build -t cryptoscope . && docker run -p 3000:3000 -v cryptoscope-data:/data cryptoscope` (корневой `Dockerfile` собирает `cryptoscope/`)
- **Деплой: Railway**, порт 3000, persistent volume примонтирован к `/data`
- `cryptoscope/start.sh` — entrypoint: rebuild БД из seed-CSV при пустом volume → миграция crypto→MEXC → compute_analysis при пустых pairs → загрузка ru/br/id/au → extension feed → фоновый daily-loop (06:30 UTC обновление цен + пересчёт; 16:00 UTC вечерние посты) → uvicorn `app.main:app`
- Обновление данных: крипта — MEXC public API, US/br/id/au — Yahoo (yfinance), RU — MOEX. Twelve Data больше не используется (ключ в конфиге остался как легаси)
- Env: `DB_PATH` (по умолчанию `/data/market.db`), `PORT` (3000), `APP_VARIANT` (global/br/id), `ENABLED_MARKETS`, `TWELVEDATA_API_KEY` (не нужен), Supabase/Resend для auth, PayAnyWay/PayPal для платежей, Telegram для контента — см. `app/config.py`
- CI: GitHub Actions (`.github/workflows/tests.yml`) — pytest + ruff (линт non-blocking) на push/PR

## Stack

- **Python 3.11**, FastAPI + Jinja2 (HTMX на фронте), uvicorn
- pandas / numpy / scipy / statsmodels
- SQLite через aiosqlite (`/data/market.db`)
- Тесты: pytest + pytest-asyncio (`cryptoscope/tests/`, ~45 файлов), `cd cryptoscope && pytest tests/`
- Линт: ruff (конфиг в `cryptoscope/pyproject.toml`)

## Where things live

Всё актуальное — в `cryptoscope/`:

- `app/main.py` — точка входа: middleware подписки + фильтра рынков, lifespan (MEXC-поллер, short-term monitor)
- `app/api/` — роутеры: ui_routes (`/tab/*` HTMX-партиалы), signals, favorites, auth, payments, crypto_picks, market_regime, short_term, scanners, charts, health, public_extension
- `app/core/` — математика: cointegration (Engle-Granger, z-score), signals (порог |Z|≥2, разворот z_turning), risk (стабильность коинтеграции, рыночный режим), backtest (out-of-sample, net-PnL после комиссий), calculator (P&L), short_term_lab / crypto_picks / reversal_lab / market_regime / scanners
- `app/data/` — источники: mexc (spot/futures/intraday), moex, yahoo, региональные
- `app/db/` — schema.py (миграции колонок), database.py
- `app/templates/` + `app/static/` — Jinja2 + CSS/JS
- `scripts/` (внутри cryptoscope) — build_db, compute_analysis (ежедневный пересчёт пар), daily_update, load_* рынков
- `data/` — seed-CSV (3 года daily + hourly 6 монет), запекаются в образ

**Легаси (не относится к Python-приложению):** R-скрипты в корневом `scripts/`, `artifacts/crypto-analyzer/app.R` (старая R/Shiny версия), TS-скаффолд (`lib/`, `artifacts/api-server`, `mockup-sandbox`, pnpm-файлы), `agent.md` этот раздел заменяет. Расширения браузера — `extensions/meanx-br`, `extensions/meanx-id`.

## Architecture decisions

- **Данные baked в образ + volume поверх**: seed-CSV → `/opt/seed/`, при первом запуске (или пустой/битой БД) `start.sh` пересобирает `market.db`, затем считает пары. Шаги инициализации падение не прерывают (`|| echo`) — приложение стартует деградированным, `/health/ready` показывает пробел, daily-loop долечивает.
- **Предрасчёт, не on-demand**: `scripts/compute_analysis.py` пишет все пары в таблицу `pairs` (снимок, заменяется по рынку только после полного успешного расчёта). UI читает `pairs`, тяжёлые вещи (график спреда, backtest) — on-demand.
- **Сигнал = |Z|≥2 (или прогноз) И подтверждённая коинтеграция**. Жёсткий контроль: `guard_signal` блокирует нестабильные пары («Наблюдение»), п. July 2026 ужесточил это до почти нуля сигналов — с Aug 2026 смягчено rolling-окном z-score.
- **Rolling z-score окно 120 дней** + hedge ratio с того же окна (`Z_SCORE_WINDOW`); коинтеграционный тест — на длинном окне с `COINT_MAX_PVALUE = 0.01` (контроль множественного тестирования: 4950 крипто-пар × p≤0.05 ≈ 250 ложных срабатываний).
- **Стабильность коинтеграции**: crypto/ru — окна (60,120,252), минимум 2, свежие 60д обязательны; equities — ослаблено (120,252, минимум 1) — иначе ноль сигналов.
- **Разворотный вход (soft)**: сигнал при расширяющемся спреде не скрывается, но сила капится «Формируется» + risk_reason «Спред ещё расширяется».
- **Backtest честный**: out-of-sample (70/30), net-PnL после 4×taker fee + фандинг 0.01%/8ч (crypto). `backtest_avg_net_pnl_pct` показывается как «после комиссий и фандинга».
- **Score** = |corr| + 0.3·stability% + 0.3 (halflife 5–60) + 0.2·(win_rate−50)/50 при валидированном бэктесте.
- **Кластеризация ног**: активные сигналы с общим тикером помечаются («Концентрация экспозиции: BTC ×N»).

## Product

Тёмная тема, брендинг MEANX. Вкладки: Сигналы (все/Corr Breakdown/Momentum/Drawdown + Наблюдение при пустых сигналах), Портфель (favorites), Данные; admin: Short-Term, Alpha. Региональные варианты: `APP_VARIANT=br|id`. Монетизация: magic-link auth (Supabase/Resend), trial 3 дня, подписки PayAnyWay/PayPal, access-gate middleware на `/tab/*` и `/api/*`.

## Gotchas

- **Пайплайн сигналов**: daily_update (06:30 UTC) → цены → compute_analysis → `pairs` + `signals`. Если день пропущен — пары протухшие, но UI не падает; `/health/ready` покажет `analysis is stale`.
- **Дублирование `_backtest_metrics`**: живёт в `app/api/ui_routes.py` и `app/api/signals.py` — держать синхронно.
- **`.gitignore`**: `*.db` игнорит все базы; `/scripts/` (якорь) игнорит только корневые R-скрипты. Ранее был `scripts/` без якоря — молча блокировал новые файлы в `cryptoscope/scripts/`.
- **API-ключ утёк в git-историю**: захардкожен в легаси R-скриптах (`scripts/fetch_*.R`, `54ebd565...`). Ротировать ключ, историю переписать.
- **start.sh**: шаги инициализации обёрнуты `|| echo` — падение любого не роняет деплой.
- **Volume vs build-time DB**: Railway- volume скрывает build-time базу; seed в `/opt/seed/` вне volume, пересборка при первом запуске.

## Pointers

- Тикеры: `app/data/tickers.py` (source of truth; ca/my удалены из продукта)
- Схема БД и миграции: `app/db/schema.py`
- Логика пар/сигналов: `scripts/compute_analysis.py` (source of truth) + `app/core/*`
- Пороги расчёта: `Z_SCORE_WINDOW=120`, `COINT_MAX_PVALUE=0.01` в compute_analysis.py; вход |Z|≥2 в `app/core/signals.py`
- Health: `/health/live`, `/health/ready` (проверяет свежесть цен/анализа/фидов по каждому рынку)
- Запуск тестов: `cd cryptoscope && pytest tests/`
