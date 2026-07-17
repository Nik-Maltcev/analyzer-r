# Crypto Telegram and Threads automation

The workflow publishes at most one new crypto scanner signal for each market data
date and posts one daily reply for every active publication. It never processes
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
CONTENT_THREADS_API_VERSION=v1.0
CONTENT_THREADS_DEPLOY_PREVIEW_ENABLED=false
APP_BASE_URL=https://www.meanx.pro
```

The token is generated in Meta for Developers after the public Threads profile
accepts its tester invitation. Keep it only in Railway variables. Meta downloads
the generated card through `/api/public/content/cards/<filename>`, which can be
proxied by Yandex API Gateway to Railway.

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

For temporary visual testing, set `CONTENT_DEPLOY_PREVIEW_ENABLED=true`. Each
service deploy republishes the latest active signal and makes that new message the
parent for future daily updates. The scheduled daily run does not create preview
duplicates. Set the variable back to `false` after approving the post design.
