# 📈 E*TRADE Stock Monitor

A self-hosted Python bot that reads your E*TRADE portfolio, scrapes cross-portfolio news (including macro/political), analyses everything with AI, and emails you a **PM-level briefing** twice a day — pre-market (8:30 AM ET) and post-market (4:30 PM ET), Monday–Friday.

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## What you get in each email

| Section | Details |
|---|---|
| **Header** | Portfolio value (or % view), day gain/loss |
| **AI Market Read** | 1-sentence overall assessment + macro note |
| **Top 5 News** | Cross-portfolio ranked by recency, your holding weight, and market impact |
| **PM Stock Calls** | Per-stock: estimated move %, trend narrative, BUY/SELL/HOLD/TRIM with specific action, stop-loss, price target |
| **Positions table** | Price, day %, portfolio weight %, P&L % |

News sources: Yahoo Finance, Google News, Reuters, AP Business, MarketWatch, and macro feeds covering tariffs, trade war, Fed, and geopolitics.

---

## Prerequisites

- Python 3.11+
- An **E\*TRADE brokerage account** (individual or IRA)
- A **Gmail account** (for sending emails via Gmail API)
- One of the following for AI analysis:
  - **Google Gemini API key** — free tier, 200 requests/day *(recommended)*
  - **Anthropic Claude API key** — paid, ~$0.001/call with Haiku

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/YOUR_USERNAME/etrade-stock-monitor.git
cd etrade-stock-monitor

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. E\*TRADE API credentials

1. Go to [developer.etrade.com](https://developer.etrade.com/getting-started) and sign in with your brokerage account
2. Click **Get API Key** → fill out the form (application type: Individual)
3. You'll receive a **Consumer Key** and **Consumer Secret** — save these
4. Start with `ETRADE_ENVIRONMENT=sandbox` for testing with simulated data
5. When ready for real data, request **production access** on the same page (usually approved in 1–2 business days)

### 3. LLM API key (choose one)

**Option A — Google Gemini (free, recommended)**
1. Go to [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
2. Click **Create API key** — no credit card required
3. Free tier gives 200 requests/day — more than enough for twice-daily emails

**Option B — Anthropic Claude (paid)**
1. Go to [console.anthropic.com](https://console.anthropic.com/) and create an API key
2. Claude Haiku costs ~$0.001 per email. Set `DAILY_SPEND_LIMIT_USD` in `.env` as a safety cap

### 4. Gmail API (for sending emails via OAuth2)

1. Go to [console.cloud.google.com](https://console.cloud.google.com/) and create a new project (e.g. "Stock Monitor")
2. Enable the **Gmail API**: APIs & Services → Enable APIs → search "Gmail API" → Enable
3. Create OAuth credentials: APIs & Services → Credentials → Create Credentials → **OAuth 2.0 Client ID**
   - Application type: **Desktop app**
4. Download the JSON → **save it as `credentials.json` in the project root**
5. Add your Gmail as a test user: APIs & Services → OAuth consent screen → **Audience** → Add Users

> On first run a browser opens for one-time authorization. After that the token is cached locally — no further interaction needed.

### 5. Configure your environment

```bash
cp .env.example .env
```

Edit `.env` with your values:

```env
# E*TRADE
ETRADE_CONSUMER_KEY=your_key_here
ETRADE_CONSUMER_SECRET=your_secret_here
ETRADE_ENVIRONMENT=sandbox        # change to "live" when ready

# LLM (pick one)
LLM_BACKEND=gemini                # or "claude" or "fallback"
GEMINI_API_KEY=your_gemini_key_here
# ANTHROPIC_API_KEY=your_anthropic_key_here

# Email
EMAIL_BACKEND=gmail_api
GMAIL_ADDRESS=you@gmail.com
RECIPIENT_EMAIL=you@gmail.com     # can be any email address
```

### 6. First run

```bash
python -m src.main
```

On first run the script will:
1. Open a browser for **E\*TRADE authorization** — log in and paste the PIN code shown
2. Open a browser for **Gmail authorization** — sign in and allow access
3. Fetch your portfolio, scrape news, run AI analysis, and send the email immediately

Both tokens are cached after the first run. E\*TRADE tokens expire at midnight ET, so you'll re-authorize once per day (see [Roadmap](ROADMAP.md) for planned improvements).

### 7. Run the scheduler (automated daily emails)

```bash
python -m src.main --schedule
```

This keeps running in the foreground and sends emails at 8:30 AM and 4:30 PM ET on weekdays. To run as a background process:

```bash
nohup python -m src.main --schedule > logs/scheduler.log 2>&1 &
```

Or use `launchd` (macOS) / `systemd` (Linux) / Task Scheduler (Windows) to start it automatically on boot.

---

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `ETRADE_ENVIRONMENT` | `sandbox` | `sandbox` or `live` |
| `LLM_BACKEND` | `gemini` | `gemini`, `claude`, or `fallback` |
| `TOP_NEWS_COUNT` | `5` | Articles per email |
| `DAILY_SPEND_LIMIT_USD` | `0.50` | Max Claude API spend per calendar day |
| `HIDE_ACCOUNT_VALUE` | `false` | Show % performance instead of $ totals in email |
| `PRE_MARKET_HOUR` | `8` | Morning email hour (ET, 24h) |
| `POST_MARKET_HOUR` | `16` | Evening email hour (ET, 24h) |

---

## Project structure

```
etrade-stock-monitor/
├── src/
│   ├── etrade/
│   │   ├── auth.py          # E*TRADE OAuth 1.0a + daily token caching
│   │   └── portfolio.py     # Positions reader, P&L calculator
│   ├── news/
│   │   └── scraper.py       # Yahoo Finance + RSS + macro feeds + cross-portfolio ranker
│   ├── analysis/
│   │   └── analyzer.py      # Gemini / Claude PM-level analysis engine
│   ├── email/
│   │   └── sender.py        # Mobile-responsive HTML email + Gmail API
│   └── utils/
│       ├── config.py        # Settings via pydantic-settings
│       └── logger.py        # Loguru structured logging
├── tests/                   # pytest suite
├── .env.example             # Config template (safe to commit)
├── requirements.txt
└── README.md
```

---

## Running tests

```bash
pytest tests/ -v
```

---

## Security

- **Never commit `.env`** — it contains your API keys (already in `.gitignore`)
- `credentials.json` and `gmail_token.json` are also gitignored
- `.etrade_token_cache.json` is gitignored
- All analysis runs locally; only the news text is sent to the LLM API you configure

---

## License

MIT — see [LICENSE](LICENSE)
