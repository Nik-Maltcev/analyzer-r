"""Ticker lists for all markets."""

CRYPTO_TICKERS = [
    "BTC/USD", "ETH/USD", "BNB/USD", "SOL/USD", "XRP/USD",
    "ADA/USD", "DOGE/USD", "AVAX/USD", "DOT/USD", "MATIC/USD",
    "LINK/USD", "UNI/USD", "ATOM/USD", "LTC/USD", "FIL/USD",
    "NEAR/USD", "APT/USD", "ARB/USD", "OP/USD", "ICP/USD",
    "HBAR/USD", "VET/USD", "ALGO/USD", "FTM/USD", "SAND/USD",
    "MANA/USD", "AXS/USD", "AAVE/USD", "GRT/USD", "EOS/USD",
    "THETA/USD", "XTZ/USD", "EGLD/USD", "FLOW/USD", "CHZ/USD",
    "CRV/USD", "LDO/USD", "RUNE/USD", "INJ/USD", "IMX/USD",
    "SUI/USD", "SEI/USD", "TIA/USD", "STX/USD", "RENDER/USD",
    "FET/USD", "WLD/USD", "PEPE/USD", "SHIB/USD", "FLOKI/USD",
    "ENS/USD", "MKR/USD", "SNX/USD", "COMP/USD", "1INCH/USD",
    "SUSHI/USD", "YFI/USD", "BAL/USD", "DYDX/USD", "GMX/USD",
    "PENDLE/USD", "JUP/USD", "W/USD", "STRK/USD", "ZK/USD",
    "TRX/USD", "TON/USD", "KAS/USD", "TAO/USD", "FLR/USD",
    "XLM/USD", "BCH/USD", "ETC/USD", "ZEC/USD", "DASH/USD",
    "NEO/USD", "WAVES/USD", "IOTA/USD", "ZIL/USD", "ONE/USD",
    "ENJ/USD", "GALA/USD", "ROSE/USD", "CELO/USD", "KAVA/USD",
    "OSMO/USD", "MINA/USD", "QNT/USD", "XMR/USD", "AR/USD",
    "AGIX/USD", "OCEAN/USD", "RNDR/USD", "CFX/USD", "BLUR/USD",
    "APE/USD", "CRO/USD", "LUNC/USD", "JASMY/USD", "MASK/USD",
]

STOCK_TICKERS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "TSLA", "META", "BRK.B", "JPM", "V",
    "UNH", "JNJ", "WMT", "MA", "PG", "HD", "XOM", "CVX", "MRK", "ABBV",
    "KO", "PEP", "COST", "AVGO", "TMO", "MCD", "CSCO", "ACN", "ABT", "DHR",
    "NEE", "LIN", "TXN", "PM", "UNP", "CRM", "AMD", "INTC", "NFLX", "QCOM",
    "SPY", "QQQ", "IWM", "DIA", "VTI", "XLF", "XLE", "XLK", "GLD", "TLT",
]

RU_TICKERS = [
    "SBER", "GAZP", "LKOH", "GMKN", "ROSN", "VTBR",
    "TATN", "NVTK", "ALRS", "MTSS", "MGNT", "CHMF",
    "SNGS", "AFLT", "MOEX", "PHOR", "PLZL", "TCSG",
]

ALL_MARKETS = {
    "crypto": CRYPTO_TICKERS,
    "stocks": STOCK_TICKERS,
    "ru": RU_TICKERS,
}

MARKET_NAMES = {
    "crypto": "Crypto",
    "stocks": "Акции/ETF",
    "ru": "RU",
}
