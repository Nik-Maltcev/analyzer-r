#!/usr/bin/env Rscript
# ── Ensure favorites table exists in DB (for existing volumes) ──────────────
# Called by start.sh. Creates table if missing, otherwise does nothing.
library(RSQLite)

DB_PATH <- Sys.getenv("DB_PATH", "/data/market.db")

if (!file.exists(DB_PATH)) stop("DB not found: ", DB_PATH)

con <- dbConnect(SQLite(), DB_PATH)
on.exit(dbDisconnect(con))

tables <- dbGetQuery(con, "SELECT name FROM sqlite_master WHERE type='table' AND name='favorites'")$name
if ("favorites" %in% tables) {
  cat("favorites table already exists\n")
  quit("no", 0)
}

cat("Creating favorites table...\n")
dbExecute(con, "
  CREATE TABLE IF NOT EXISTS favorites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair         TEXT NOT NULL,
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
")
cat("favorites table created\n")
