# Polymarket Copy-Trade Radar — PandaStack Deployment

## Recommended shared volume layout
Mount a persistent disk at `/data` for both services, then set:
- Static Site publish dir: `/data`
- `PM_OUTPUT_HTML=/data/copyable_wallets.html`
- `PM_HTML_REPORT=/data/copyable_wallets.html`
- `PM_DB_PATH=/data/sqlite.db`

## Environment variables
- `TELEGRAM_BOT_TOKEN`: from @BotFather
- `PM_MINI_APP_URL`: HTTPS URL of the static dashboard
- `PM_DB_PATH`: optional, default `copyable_wallets.db`
- `PM_OUTPUT_HTML`: optional, default `copyable_wallets.html`
- `PM_HTML_REPORT`: optional, default `copyable_wallets.html`
- `PM_HEALTH_PORT`: optional, default `8080`
- `TELEGRAM_ALLOWED_USERS`: optional comma-separated IDs

## Deploy steps
### 1. Static Site (Mini App dashboard)
- New Service → Static Site → repo → publish dir `/data`

### 2. Web Service (Telegram bot)
- New Service → Background Worker / Web Service
- Start command: `python3 pm_telegram_bot.py`
- Attach persistent disk at `/data` if available
- Health check: GET `/ping` on `PM_HEALTH_PORT`
