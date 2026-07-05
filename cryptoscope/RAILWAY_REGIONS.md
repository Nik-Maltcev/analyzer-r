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
AUTH_LEGACY_OWNER_EMAIL=owner@example.com
PAYPAL_CLIENT_ID=...
PAYPAL_CLIENT_SECRET=...
PAYPAL_MODE=live
PAYPAL_CURRENCY=USD
PAYPAL_MONTH_AMOUNT=9.90
PAYPAL_YEAR_AMOUNT=99.00
```

The interface is in Brazilian Portuguese. Enabled markets: crypto, US
stocks/ETF, and B3.

## Indonesia

```text
APP_VARIANT=id
APP_BASE_URL=https://indonesia-domain.example
DB_PATH=/data/market.db
AUTH_LEGACY_OWNER_EMAIL=owner@example.com
PAYPAL_CLIENT_ID=...
PAYPAL_CLIENT_SECRET=...
PAYPAL_MODE=live
PAYPAL_CURRENCY=USD
PAYPAL_MONTH_AMOUNT=9.90
PAYPAL_YEAR_AMOUNT=99.00
```

The default language is Indonesian, with an English switch. Enabled markets:
crypto, US stocks/ETF, and IDX.

## Shared secrets

Copy the required API secrets to every service:

```text
RESEND_API_KEY=...
RESEND_FROM_EMAIL=...
TWELVEDATA_API_KEY=...
```

Use a verified sender such as `MEANX <login@your-domain.example>`. Legacy
`APP_NAME=CryptoScope...` values are migrated to `MEANX` automatically, but
removing them from Railway keeps the configuration clear.

`ENABLED_MARKETS`, locale, timezone, and currency are derived automatically
from `APP_VARIANT`. Set them only when a service needs custom behavior. Do not
attach one Railway volume to multiple services: SQLite, users, payment orders,
and positions must remain isolated per domain.

Brazil and Indonesia use server-created PayPal Orders. The public Client ID is
used by the JavaScript SDK; the Client Secret remains server-side. A completed
capture is checked against the stored user, plan, amount, and currency before
access is extended.

Keep all three services always on. Each service runs its own Binance stream
and daily data update, matching the current deployment model.

When Resend is configured, every edition enforces the three-day trial and paid
subscription access. `AUTH_LEGACY_OWNER_EMAIL` remains unrestricted and should
be the operator's own login email.
