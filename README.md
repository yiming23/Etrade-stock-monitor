# 📈 E*TRADE Stock Monitor

A self-hosted Python bot that reads your E*TRADE portfolio, scrapes cross-portfolio news (including macro/political), analyses everything with AI, and emails you a **PM-level briefing** three times a day — pre-market, mid-day, and post-market — Monday–Friday.

![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## What you get in each email

| Time (ET) | Focus |
|---|---|
| **8:30 AM** Pre-Market | Overnight catalysts, opening gap direction, first-hour trade plan |
| **12:00 PM** Mid-Day | Morning recap, what changed vs the open thesis, afternoon calls |
| **4:30 PM** Post-Market | Day summary, upcoming earnings dates, Fed/macro events, geopolitical watch, hold-through-earnings recommendations |

Every email includes:
- AI market read + macro note
- Top 5 news ranked by recency, your holding weight, and market impact
- Per-stock PM calls: estimated move %, trend narrative, BUY/SELL/HOLD/TRIM, stop-loss, price target
- Positions table with live prices, day %, portfolio weight %, P&L %

---

## Prerequisites

- Python 3.9+ (3.11 recommended)
- An **E\*TRADE brokerage account**
- A **Gmail account** for sending emails
- One of the following for AI analysis:
  - **Google Gemini API key** — free tier, 200 requests/day *(recommended)*
  - **Anthropic Claude API key** — paid, ~$0.001/call with Haiku
- A **Telegram account** for auth PIN delivery and alerts *(strongly recommended)*

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/YOUR_USERNAME/etrade-stock-monitor.git
cd etrade-stock-monitor

python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. E\*TRADE API credentials

1. Go to [developer.etrade.com](https://developer.etrade.com/getting-started) and sign in
2. Click **Get API Key** → fill out the form (Individual application)
3. Save your **Consumer Key** and **Consumer Secret**
4. Start with `ETRADE_ENVIRONMENT=sandbox` for testing with simulated data
5. Request **production access** when ready for real data (approved in 1–2 business days)

### 3. LLM API key

**Option A — Google Gemini (free)**
1. Go to [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
2. Click **Create API key** — no credit card required

**Option B — Anthropic Claude (paid)**
1. Go to [console.anthropic.com](https://console.anthropic.com/) and create an API key
2. Set `DAILY_SPEND_LIMIT_USD` in `.env` as a safety cap (~$0.001/email with Haiku)

### 4. Gmail API

1. Go to [console.cloud.google.com](https://console.cloud.google.com/) → create a project
2. Enable **Gmail API**: APIs & Services → Enable APIs → search "Gmail API"
3. Create credentials: APIs & Services → Credentials → **OAuth 2.0 Client ID** → Desktop app
4. Download the JSON → save as `credentials.json` in the project root
5. Add your Gmail as a test user: OAuth consent screen → **Audience** → Add Users

### 5. Telegram bot (for auth PIN + alerts)

1. Message **@BotFather** on Telegram → `/newbot` → follow prompts → copy the token
2. Start a chat with your new bot (send `/start`)
3. Get your Chat ID — open this URL in a browser:
   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```
   Find `"chat":{"id":` in the response — that number is your Chat ID

### 6. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

```env
# E*TRADE
ETRADE_CONSUMER_KEY=your_key_here
ETRADE_CONSUMER_SECRET=your_secret_here
ETRADE_ENVIRONMENT=sandbox        # change to "live" when ready

# LLM
LLM_BACKEND=gemini                # or "claude"
GEMINI_API_KEY=your_gemini_key_here

# Email
EMAIL_BACKEND=gmail_api
GMAIL_ADDRESS=you@gmail.com
RECIPIENT_EMAIL=you@gmail.com

# Telegram (strongly recommended)
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
```

### 7. First run

```bash
source .venv/bin/activate
python -m src.main --once --type pre_market
```

On first run:
1. **E\*TRADE auth** — a Telegram message arrives with a button. Tap it, log in, reply with the PIN. *(Or a browser opens if Telegram is not configured.)*
2. **Gmail auth** — a browser opens for one-time OAuth authorization
3. Portfolio loads, news is scraped, AI analysis runs, email is sent

After the first run, the portfolio is cached. Future runs reuse it and only fetch live prices from yfinance — no E\*TRADE auth needed until the weekly refresh.

---

## Running automatically on macOS

### First-time setup

```bash
./restart.sh
```

That's it. The script:
1. Installs/updates dependencies
2. Checks for syntax errors
3. Copies the plist to `~/Library/LaunchAgents/`
4. Reloads launchd and verifies the process started

### After every code update

```bash
./restart.sh
```

Same command — picks up all changes and restarts cleanly.

### Useful commands

```bash
# Watch live logs
tail -f logs/launchd_stdout.log

# Stop
launchctl unload ~/Library/LaunchAgents/com.stockmonitor.plist

# Check if running (first column should be a PID, not "-")
launchctl list | grep stockmonitor
```

> The process runs continuously in the background. macOS launchd keeps it alive automatically — if it crashes, it restarts within 30 seconds. The screen can be locked; the process keeps running as long as you're logged in.

---

## Telegram commands

Once the scheduler is running, send these to your bot anytime:

| Command | Action |
|---|---|
| `/auth` | Re-authorize E\*TRADE immediately (e.g. if Sunday auth timed out) |
| `/status` | Show portfolio cache info — last refresh time and current holdings |
| `/help` | List available commands |

---

## Weekly E\*TRADE re-authorization

E\*TRADE tokens expire daily but the **portfolio cache** (symbol, quantity, cost basis) is refreshed once a week. Every **Sunday at 12:00 PM ET** the bot sends you a Telegram message with an auth button. Tap it, log in, reply with the PIN — done until next Sunday.

If you miss the Sunday window, send `/auth` anytime to re-authorize on demand.

If the Mac was off on Sunday, the `misfire_grace_time` setting means the job fires within 1 hour of the Mac coming back online.

---

## Configuration reference

All settings in `.env`:

| Variable | Default | Description |
|---|---|---|
| `ETRADE_ENVIRONMENT` | `sandbox` | `sandbox` or `live` |
| `LLM_BACKEND` | `gemini` | `gemini`, `claude`, or `fallback` |
| `TOP_NEWS_COUNT` | `5` | Articles per email |
| `DAILY_SPEND_LIMIT_USD` | `0.50` | Max Claude API spend per day |
| `HIDE_ACCOUNT_VALUE` | `true` | Show % instead of $ in email header |
| `PRE_MARKET_HOUR` | `8` | Morning email hour (ET) |
| `MID_MARKET_HOUR` | `12` | Midday email hour (ET) |
| `POST_MARKET_HOUR` | `16` | Evening email hour (ET) |
| `PORTFOLIO_REFRESH_DAY` | `sun` | Day of weekly E\*TRADE re-auth |
| `PORTFOLIO_REFRESH_HOUR` | `12` | Hour of weekly re-auth (ET) |
| `PORTFOLIO_CACHE_DAYS` | `8` | Fallback: force re-auth if cache older than N days |
| `TELEGRAM_ENABLED` | `false` | Enable Telegram bot |
| `TELEGRAM_AUTH_TIMEOUT_SECS` | `300` | Seconds to wait for PIN before giving up |

---

## Testing individual reports

```bash
# Run each report type immediately (uses cached portfolio + live yfinance prices)
python -m src.main --once --type pre_market
python -m src.main --once --type mid_market
python -m src.main --once --type post_market

# Force a full E*TRADE re-auth and refresh portfolio cache
python -m src.main --refresh-portfolio
```

---

## Project structure

```
etrade-stock-monitor/
├── src/
│   ├── etrade/
│   │   ├── auth.py          # E*TRADE OAuth 1.0a + Telegram PIN delivery
│   │   └── portfolio.py     # Live fetch + weekly cache + yfinance enrichment
│   ├── news/
│   │   └── scraper.py       # Yahoo Finance + RSS + macro feeds + ranker
│   ├── analysis/
│   │   └── analyzer.py      # Three report-type-aware AI prompts
│   ├── email/
│   │   └── sender.py        # Mobile-responsive HTML email + Gmail API
│   └── utils/
│       ├── config.py        # All settings via pydantic-settings
│       ├── logger.py        # Loguru structured logging
│       └── telegram_bot.py  # Bot: auth flow + command listener
├── com.stockmonitor.plist   # macOS LaunchAgent config
├── restart.sh               # One-command deploy/restart script
├── .env.example             # Config template
├── requirements.txt
└── README.md
```

---

## Security

- **Never commit `.env`** — contains all API keys (already in `.gitignore`)
- `credentials.json`, `gmail_token.json`, `.etrade_token_cache.json`, `.portfolio_cache.json` are all gitignored
- All analysis runs locally; only news text is sent to the LLM API you configure

---

## License

MIT — see [LICENSE](LICENSE)
