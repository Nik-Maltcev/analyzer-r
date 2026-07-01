# Railway regional services

Create three Railway services from the same repository and branch. Each
service should use the root `Dockerfile`, its own domain, and its own volume
mounted at `/data`. All editions use the `MEANX` brand.

## Global

```text
APP_VARIANT=global
APP_BASE_URL=https://global-domain.example
DB_PATH=/data/market.db
```

Enabled markets: crypto, US stocks/ETF, Russia, Brazil, and Indonesia.

## Brazil

```text
APP_VARIANT=br
APP_BASE_URL=https://brazil-domain.example
DB_PATH=/data/market.db
```

The interface is in Brazilian Portuguese. Enabled markets: crypto, US
stocks/ETF, and B3.

## Indonesia

```text
APP_VARIANT=id
APP_BASE_URL=https://indonesia-domain.example
DB_PATH=/data/market.db
```

The default language is Indonesian, with an English switch. Enabled markets:
crypto, US stocks/ETF, and IDX.

## Shared secrets

Copy the required API secrets to every service:

```text
RESEND_API_KEY=...
RESEND_FROM_EMAIL=...
DEEPSEEK_API_KEY=...
TWELVEDATA_API_KEY=...
```

Use a verified sender such as `MEANX <login@your-domain.example>`. Legacy
`APP_NAME=CryptoScope...` values are migrated to `MEANX` automatically, but
removing them from Railway keeps the configuration clear.

`ENABLED_MARKETS` is derived automatically from `APP_VARIANT`. Set it only
when a service needs a custom market list. Do not attach one Railway volume to
multiple services: SQLite and user accounts must remain isolated per domain.

Keep all three services always on. Each service runs its own Binance stream
and daily data update, matching the current deployment model.

The Brazil and Indonesia editions intentionally hide the current RUB/PayPal
pricing block until regional prices and payment accounts are configured.
