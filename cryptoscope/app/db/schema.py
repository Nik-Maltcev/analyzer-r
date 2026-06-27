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
    is_coint    INTEGER,
    hedge_ratio REAL,
    score       REAL,
    z_now       REAL,
    z_forecast  REAL,
    signal      TEXT,
    signal_type TEXT,
    strength    TEXT,
    signal_started_at TEXT,
    computed_at TEXT DEFAULT (datetime('now')),
    UNIQUE (market, ticker_a, ticker_b)
)
"""

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
    ticker_a     TEXT NOT NULL,
    ticker_b     TEXT NOT NULL,
    signal       TEXT,
    signal_type  TEXT,
    z_at_entry   REAL,
    price_a_entry REAL,
    price_b_entry REAL,
    entry_time   TEXT,
    exit_time    TEXT,
    exit_price_a REAL,
    exit_price_b REAL,
    exit_pnl_pct  REAL,
    status       TEXT DEFAULT 'active',
    halflife     INTEGER,
    corr         REAL,
    user_id      TEXT DEFAULT 'local',
    created_at   TEXT DEFAULT (datetime('now'))
)
"""

ALL_TABLES_SQL = [
    CREATE_PRICES,
    CREATE_PAIRS,
    CREATE_SIGNALS,
    CREATE_UPDATE_LOG,
    CREATE_HOURLY_PRICES,
    CREATE_FAVORITES,
]

ALL_INDICES_SQL = (
    CREATE_PRICES_INDICES
    + CREATE_PAIRS_INDICES
    + CREATE_HOURLY_INDICES
)
