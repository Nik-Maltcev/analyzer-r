# Crypto Telegram and Threads automation

The workflow publishes at most one new crypto scanner signal for each market data
date and at most one channel update per day. Every active publication is still
recalculated and stored. Closure or reversal updates have priority; otherwise the
signal with the largest absolute move is selected. The workflow never processes
equities or regional markets.

## Railway variables

Required:

```text
CONTENT_AUTOMATION_ENABLED=true
CONTENT_TELEGRAM_BOT_TOKEN=<Telegram BotFather token>
CONTENT_TELEGRAM_CHAT_ID=<channel id such as -1001234567890 or @channel_name>
CONTENT_OPENROUTER_API_KEY=<OpenRouter key>
CONTENT_OPENROUTER_TEXT_MODEL=<Claude model slug enabled in OpenRouter>
CONTENT_OPENROUTER_IMAGE_MODEL=<Gemini image model slug enabled in OpenRouter>
```

Optional:

```text
CONTENT_BOT_USER_ID=content-bot
CONTENT_CARD_DIR=/data/content_cards
CONTENT_REPEAT_TICKER_DAYS=30
CONTENT_DEPLOY_PREVIEW_ENABLED=false
```

The Telegram bot must be an administrator of the channel with permission to post.
If the OpenRouter key or either model is absent, the workflow uses a deterministic
text/card fallback. Telegram configuration is mandatory when automation is enabled.

Optional Threads publishing for the same signal and its daily updates:

```text
CONTENT_THREADS_ENABLED=true
CONTENT_THREADS_ACCESS_TOKEN=<long-lived Threads tester token>
CONTENT_THREADS_API_VERSION=
CONTENT_PUBLIC_ASSET_BASE_URL=
APP_BASE_URL=https://www.meanx.pro
```

The token is generated in Meta for Developers after the public Threads profile
accepts its tester invitation. Keep it only in Railway variables. Meta downloads
the generated card through `/api/public/content/cards/<filename>`. On Railway,
`RAILWAY_PUBLIC_DOMAIN` is used automatically so Meta fetches the JPEG directly
from the application instead of through a regional reverse proxy. Inside Railway,
this direct domain always takes priority over `CONTENT_PUBLIC_ASSET_BASE_URL`. Set
the optional variable only when the automatic Railway domain is unavailable.

## Lifecycle

1. Update every active publication using the latest completed crypto candle.
2. Close monitoring when the scanner condition disappears or reverses.
3. Retry an unfinished draft before selecting another signal.
4. Select the strongest active high-confidence Momentum or Drawdown signal.
5. Add it to the isolated `content-bot` favorites portfolio.
6. Generate a card and caption, publish them, and store the Telegram message id.
7. When Threads is enabled, publish the same card and store its Threads post id.

Updates are replies to the original post. A unique database constraint and market
data date make deploy/startup retries idempotent.

## Production schedule

The Railway service loop uses UTC and runs every day, including weekends:

```text
06:30-08:00 UTC / 09:30-11:00 MSK - refresh data and publish one main signal
16:00-18:00 UTC / 19:00-21:00 MSK - publish at most one active-signal update
```

Marker files in `/data` prevent duplicate runs after a service restart. The main
post uses `--main-only`; the evening run uses `--updates-only`. If no eligible
high-confidence signal or no newer market data exists, the run finishes without
posting filler content. Restarts outside these windows do not trigger late catch-up
posts. Main publications include an image in both channels. Evening updates remain
text replies in Telegram and use an update image card in Threads.

Threads image processing is retried three times. A failed image is never replaced
with a text-only Threads post; the missing publication remains eligible for the
next startup backfill.

For temporary visual testing, set `CONTENT_DEPLOY_PREVIEW_ENABLED=true`. Each
service deploy republishes the latest active signal and makes that new message the
parent for future daily updates. The scheduled daily run does not create preview
duplicates. Set the variable back to `false` after approving the post design.
