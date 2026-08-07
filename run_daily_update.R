# Local daily update wrapper — sources tickers from correct path
SCRIPTS_DIR <- "D:/Crypto-Analyzer-R/scripts"
source(file.path(SCRIPTS_DIR, "tickers.R"))

# Override DB_PATH for local use
Sys.setenv(DB_PATH = "D:/Crypto-Analyzer-R/cryptoscope/data/market.db")

# Now run the actual update
source(file.path(SCRIPTS_DIR, "daily_update.R"), local = TRUE)
