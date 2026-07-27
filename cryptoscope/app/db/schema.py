"""Database schema definitions."""

CREATE_PRICES = """
CREATE TABLE IF NOT EXISTS prices (
    ticker TEXT NOT NULL,
    date   TEXT NOT NULL,
    close  REAL NOT NULL,
    volume REAL,
    market TEXT,
    PRIMARY KEY (ticker, date)
)
"""

CREATE_PRICES_INDICES = [
    "CREATE INDEX IF NOT EXISTS idx_prices_ticker ON prices(ticker)",
    "CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(date)",
]

CREATE_PAIRS = """
CREATE TABLE IF NOT EXISTS pairs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    market      TEXT NOT NULL,
    ticker_a    TEXT NOT NULL,
    ticker_b    TEXT NOT NULL,
    corr        REAL,
    halflife    INTEGER,
    t_stat      REAL,
    coint_pvalue REAL,
    coint_critical_5pct REAL,
    is_coint    INTEGER,
    hedge_ratio REAL,
    ar_phi      REAL,
    spread_sd_pct REAL,
    backtest_trades INTEGER DEFAULT 0,
    backtest_win_rate REAL,
    backtest_avg_pnl_pct REAL,
    backtest_avg_pnl_sigma REAL,
    backtest_avg_hold_days REAL,
    backtest_validated INTEGER DEFAULT 0,
    score       REAL,
    z_now       REAL,
    z_forecast  REAL,
    z_forecast_low REAL,
    z_forecast_high REAL,
    signal      TEXT,
    signal_type TEXT,
    strength    TEXT,
    signal_eligible INTEGER DEFAULT 1,
    is_coint_stable INTEGER DEFAULT 0,
    coint_stability REAL,
    coint_windows TEXT,
    market_regime TEXT DEFAULT 'normal',
    market_volatility REAL,
    event_risk INTEGER DEFAULT 0,
    risk_reason TEXT,
    signal_started_at TEXT,
    computed_at TEXT DEFAULT (datetime('now')),
    UNIQUE (market, ticker_a, ticker_b)
)
"""

PAIR_COLUMN_MIGRATIONS = {
    "coint_pvalue": "REAL",
    "coint_critical_5pct": "REAL",
    "ar_phi": "REAL",
    "spread_sd_pct": "REAL",
    "backtest_trades": "INTEGER DEFAULT 0",
    "backtest_win_rate": "REAL",
    "backtest_avg_pnl_pct": "REAL",
    "backtest_avg_pnl_sigma": "REAL",
    "backtest_avg_hold_days": "REAL",
    "backtest_validated": "INTEGER DEFAULT 0",
    "z_forecast_low": "REAL",
    "z_forecast_high": "REAL",
    "signal_eligible": "INTEGER DEFAULT 1",
    "is_coint_stable": "INTEGER DEFAULT 0",
    "coint_stability": "REAL",
    "coint_windows": "TEXT",
    "market_regime": "TEXT DEFAULT 'normal'",
    "market_volatility": "REAL",
    "event_risk": "INTEGER DEFAULT 0",
    "risk_reason": "TEXT",
}

FAVORITE_COLUMN_MIGRATIONS = {
    "hedge_ratio_entry": "REAL",
    "spread_mean_entry": "REAL",
    "spread_sd_entry": "REAL",
    "position_kind": "TEXT DEFAULT 'pair'",
    "source": "TEXT DEFAULT 'signal'",
    "exit_net_pnl": "REAL",
    "exit_net_return_pct": "REAL",
    "exit_pair_move_pct": "REAL",
    "exit_total_cost": "REAL",
    "close_capital": "REAL",
}

CREATE_PAIRS_INDICES = [
    "CREATE INDEX IF NOT EXISTS idx_pairs_market ON pairs(market)",
    "CREATE INDEX IF NOT EXISTS idx_pairs_score ON pairs(score DESC)",
]

CREATE_SIGNALS = """
CREATE TABLE IF NOT EXISTS signals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    date       TEXT,
    ticker_a   TEXT,
    ticker_b   TEXT,
    z_score    REAL,
    z_forecast REAL,
    signal     TEXT,
    strength   TEXT,
    is_coint   INTEGER,
    corr       REAL,
    created_at TEXT DEFAULT (datetime('now'))
)
"""

CREATE_UPDATE_LOG = """
CREATE TABLE IF NOT EXISTS update_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT (datetime('now')),
    market TEXT,
    tickers_ok INTEGER,
    tickers_fail INTEGER,
    rows_added INTEGER,
    status TEXT,
    message TEXT
)
"""

CREATE_SCANNER_SIGNAL_PERIODS = """
CREATE TABLE IF NOT EXISTS scanner_signal_periods (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    market            TEXT NOT NULL,
    scanner           TEXT NOT NULL,
    signal_key        TEXT NOT NULL,
    ticker_a          TEXT NOT NULL,
    ticker_b          TEXT DEFAULT '',
    direction         TEXT NOT NULL,
    confidence        TEXT,
    strategy_admitted_date TEXT,
    strategy_confidence TEXT,
    strategy_entry_price REAL,
    strategy_entry_recorded_at TEXT,
    strategy_entry_source TEXT,
    strategy_exit_date TEXT,
    strategy_exit_price REAL,
    strategy_exit_recorded_at TEXT,
    strategy_exit_reason TEXT,
    strategy_return_pct REAL,
    strategy_cash_result REAL,
    strategy_stake REAL,
    strategy_version TEXT,
    first_seen_date   TEXT NOT NULL,
    last_seen_date    TEXT NOT NULL,
    observation_count INTEGER NOT NULL DEFAULT 1,
    status            TEXT NOT NULL DEFAULT 'active',
    ended_date        TEXT,
    close_reason      TEXT,
    closed_price      REAL,
    closed_at         TEXT,
    created_at        TEXT DEFAULT (datetime('now')),
    updated_at        TEXT DEFAULT (datetime('now')),
    UNIQUE (market, scanner, signal_key, direction, first_seen_date)
)
"""

CREATE_SCANNER_SIGNAL_INDICES = [
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_scanner_signal_active
    ON scanner_signal_periods(market, scanner, signal_key)
    WHERE status = 'active'
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_scanner_signal_history
    ON scanner_signal_periods(market, scanner, signal_key, first_seen_date)
    """,
]

CREATE_HOURLY_PRICES = """
CREATE TABLE IF NOT EXISTS hourly_prices (
    ticker    TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    date      TEXT NOT NULL,
    hour      INTEGER NOT NULL,
    open      REAL,
    high      REAL,
    low       REAL,
    close     REAL NOT NULL,
    volume    REAL,
    PRIMARY KEY (ticker, timestamp)
)
"""

CREATE_HOURLY_INDICES = [
    "CREATE INDEX IF NOT EXISTS idx_hourly_ticker ON hourly_prices(ticker)",
    "CREATE INDEX IF NOT EXISTS idx_hourly_hour ON hourly_prices(hour)",
]

CREATE_FAVORITES = """
CREATE TABLE IF NOT EXISTS favorites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair         TEXT NOT NULL,
    market       TEXT DEFAULT 'crypto',
    position_kind TEXT DEFAULT 'pair',
    source        TEXT DEFAULT 'signal',
    ticker_a     TEXT NOT NULL,
    ticker_b     TEXT NOT NULL,
    signal       TEXT,
    signal_type  TEXT,
    z_at_entry   REAL,
    hedge_ratio_entry REAL,
    spread_mean_entry REAL,
    spread_sd_entry REAL,
    price_a_entry REAL,
    price_b_entry REAL,
    entry_time   TEXT,
    exit_time    TEXT,
    exit_price_a REAL,
    exit_price_b REAL,
    exit_pnl_pct  REAL,
    exit_net_pnl REAL,
    exit_net_return_pct REAL,
    exit_pair_move_pct REAL,
    exit_total_cost REAL,
    close_capital REAL,
    status       TEXT DEFAULT 'active',
    halflife     INTEGER,
    corr         REAL,
    user_id      TEXT DEFAULT 'local',
    created_at   TEXT DEFAULT (datetime('now'))
)
"""

CREATE_AUTH_USERS = """
CREATE TABLE IF NOT EXISTS auth_users (
    id               TEXT PRIMARY KEY,
    email            TEXT NOT NULL UNIQUE,
    created_at       TEXT DEFAULT (datetime('now')),
    last_login_at    TEXT,
    trial_started_at TEXT,
    trial_ends_at    TEXT
)
"""

CREATE_AUTH_MAGIC_LINKS = """
CREATE TABLE IF NOT EXISTS auth_magic_links (
    token_hash TEXT PRIMARY KEY,
    email      TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at    TEXT,
    request_ip TEXT,
    redirect_path TEXT,
    created_at TEXT DEFAULT (datetime('now'))
)
"""

CREATE_AUTH_SESSIONS = """
CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
)
"""

CREATE_PAYMENT_NOTIFICATIONS = """
CREATE TABLE IF NOT EXISTS payment_notifications (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    provider       TEXT NOT NULL DEFAULT 'payanyway',
    transaction_id TEXT NOT NULL,
    operation_id   TEXT NOT NULL,
    account_id     TEXT NOT NULL,
    amount         TEXT NOT NULL,
    currency       TEXT NOT NULL,
    subscriber_id  TEXT,
    test_mode      INTEGER NOT NULL DEFAULT 0,
    received_at    TEXT DEFAULT (datetime('now')),
    UNIQUE(provider, operation_id)
)
"""

CREATE_PAYMENT_ORDERS = """
CREATE TABLE IF NOT EXISTS payment_orders (
    transaction_id       TEXT PRIMARY KEY,
    provider             TEXT NOT NULL DEFAULT 'payanyway',
    user_id              TEXT NOT NULL,
    plan                 TEXT NOT NULL,
    amount               TEXT NOT NULL,
    currency             TEXT NOT NULL DEFAULT 'RUB',
    status               TEXT NOT NULL DEFAULT 'pending',
    provider_operation_id TEXT,
    test_mode            INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT DEFAULT (datetime('now')),
    paid_at              TEXT,
    UNIQUE(provider_operation_id)
)
"""

PAYMENT_ORDER_COLUMN_MIGRATIONS = {
    "provider": "TEXT NOT NULL DEFAULT 'payanyway'",
}

CREATE_USER_SUBSCRIPTIONS = """
CREATE TABLE IF NOT EXISTS user_subscriptions (
    user_id             TEXT PRIMARY KEY,
    plan                TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active',
    access_until        TEXT NOT NULL,
    provider            TEXT NOT NULL,
    last_transaction_id TEXT NOT NULL,
    updated_at          TEXT DEFAULT (datetime('now'))
)
"""

CREATE_CONTENT_PUBLICATIONS = """
CREATE TABLE IF NOT EXISTS content_publications (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    market                TEXT NOT NULL DEFAULT 'crypto',
    scanner               TEXT NOT NULL,
    ticker                TEXT NOT NULL,
    direction             TEXT NOT NULL,
    confidence            TEXT NOT NULL,
    first_seen_date       TEXT NOT NULL,
    data_date             TEXT NOT NULL,
    signal_age_days       INTEGER NOT NULL DEFAULT 1,
    review_in_days        INTEGER,
    signal_start_price    REAL,
    entry_price           REAL NOT NULL,
    current_price         REAL,
    return_pct            REAL DEFAULT 0,
    status                TEXT NOT NULL DEFAULT 'draft',
    favorite_id           INTEGER,
    telegram_message_id   INTEGER,
    telegram_chat_id      TEXT,
    threads_post_id       TEXT,
    card_path             TEXT,
    initial_text          TEXT,
    last_update_text      TEXT,
    generation_payload    TEXT,
    provider_response     TEXT,
    last_update_data_date TEXT,
    threads_last_update_data_date TEXT,
    published_at          TEXT,
    closed_at             TEXT,
    created_at            TEXT DEFAULT (datetime('now')),
    updated_at            TEXT DEFAULT (datetime('now')),
    UNIQUE(market, scanner, ticker, direction, first_seen_date)
)
"""

CREATE_EXTENSION_FEED_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS extension_feed_snapshots (
    market       TEXT PRIMARY KEY,
    payload      TEXT NOT NULL,
    data_date    TEXT NOT NULL,
    generated_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

CREATE_CRYPTO_STRATEGY_TRADES = """
CREATE TABLE IF NOT EXISTS crypto_strategy_trades (
    period_id INTEGER PRIMARY KEY,
    market TEXT NOT NULL DEFAULT 'crypto',
    scanner TEXT NOT NULL,
    signal_key TEXT NOT NULL,
    ticker TEXT NOT NULL,
    direction TEXT NOT NULL,
    confidence TEXT,
    opened_on TEXT NOT NULL,
    entry_price REAL NOT NULL,
    entry_recorded_at TEXT NOT NULL,
    entry_source TEXT NOT NULL,
    closed_on TEXT NOT NULL,
    exit_price REAL NOT NULL,
    exit_recorded_at TEXT NOT NULL,
    exit_reason TEXT NOT NULL,
    return_pct REAL NOT NULL,
    cash_result REAL NOT NULL,
    stake REAL NOT NULL,
    strategy_version TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

CREATE_MOMENTUM_PORTFOLIO_RUNS = """
CREATE TABLE IF NOT EXISTS momentum_portfolio_runs (
    run_date TEXT PRIMARY KEY,
    strategy_version TEXT NOT NULL,
    status TEXT NOT NULL,
    entries_allowed INTEGER NOT NULL,
    capital REAL NOT NULL,
    allocated REAL NOT NULL,
    reserve REAL NOT NULL,
    candidates_total INTEGER NOT NULL,
    selected_total INTEGER NOT NULL,
    btc_above_sma50 INTEGER,
    btc_distance_pct REAL,
    breadth_positive INTEGER NOT NULL,
    breadth_total INTEGER NOT NULL,
    breadth_pct REAL,
    finalized_on TEXT,
    cash_result REAL,
    return_pct REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    finalized_at TEXT
)
"""

CREATE_MOMENTUM_PORTFOLIO_ALLOCATIONS = """
CREATE TABLE IF NOT EXISTS momentum_portfolio_allocations (
    run_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    rank INTEGER NOT NULL,
    confidence TEXT,
    momentum_score REAL NOT NULL,
    volatility_pct REAL NOT NULL,
    weight REAL NOT NULL,
    allocation REAL NOT NULL,
    units REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_date TEXT,
    exit_price REAL,
    return_pct REAL,
    cash_result REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (run_date, ticker),
    FOREIGN KEY (run_date) REFERENCES momentum_portfolio_runs(run_date)
)
"""

CREATE_MOMENTUM_PORTFOLIO_INDICES = [
    """
    CREATE INDEX IF NOT EXISTS idx_momentum_portfolio_finalized
    ON momentum_portfolio_runs(finalized_on, run_date)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_momentum_portfolio_allocations_ticker
    ON momentum_portfolio_allocations(ticker, run_date)
    """,
]

CREATE_CONTENT_PUBLICATION_INDICES = [
    "CREATE INDEX IF NOT EXISTS idx_content_status ON content_publications(status, data_date)",
    "CREATE INDEX IF NOT EXISTS idx_content_ticker ON content_publications(market, ticker, created_at)",
]

CREATE_AUTH_INDICES = [
    "CREATE INDEX IF NOT EXISTS idx_auth_magic_email ON auth_magic_links(email, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_auth_magic_expiry ON auth_magic_links(expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_auth_sessions_expiry ON auth_sessions(expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_payment_orders_user ON payment_orders(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_payment_orders_status ON payment_orders(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_subscriptions_expiry ON user_subscriptions(status, access_until)",
]

ALL_TABLES_SQL = [
    CREATE_PRICES,
    CREATE_PAIRS,
    CREATE_SIGNALS,
    CREATE_UPDATE_LOG,
    CREATE_SCANNER_SIGNAL_PERIODS,
    CREATE_HOURLY_PRICES,
    CREATE_FAVORITES,
    CREATE_AUTH_USERS,
    CREATE_AUTH_MAGIC_LINKS,
    CREATE_AUTH_SESSIONS,
    CREATE_PAYMENT_NOTIFICATIONS,
    CREATE_PAYMENT_ORDERS,
    CREATE_USER_SUBSCRIPTIONS,
    CREATE_CONTENT_PUBLICATIONS,
    CREATE_EXTENSION_FEED_SNAPSHOTS,
    CREATE_CRYPTO_STRATEGY_TRADES,
    CREATE_MOMENTUM_PORTFOLIO_RUNS,
    CREATE_MOMENTUM_PORTFOLIO_ALLOCATIONS,
]

ALL_INDICES_SQL = (
    CREATE_PRICES_INDICES
    + CREATE_PAIRS_INDICES
    + CREATE_SCANNER_SIGNAL_INDICES
    + CREATE_HOURLY_INDICES
    + CREATE_AUTH_INDICES
    + CREATE_CONTENT_PUBLICATION_INDICES
    + CREATE_MOMENTUM_PORTFOLIO_INDICES
)
