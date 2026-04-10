# Product Roadmap

> This document outlines what needs to be fixed before a real public launch, what features would make the product genuinely valuable, and a longer-term vision for turning this into a lightweight personal quant trading system.

---

## Phase 0 — Launch Blockers (do before making repo public)

These are not optional. They will break the experience for any new user if left unfixed.

### 0.1 Google OAuth — get out of "Testing" mode
**Problem:** Right now the Gmail OAuth app is in Testing mode. Any user who tries to authorize gets a "This app isn't verified" warning screen. Google will also block non-test users entirely.

**Fix:**
1. Go to Google Cloud Console → OAuth consent screen → change Publishing status from **Testing** to **In production**
2. Fill out the required verification form (app name, logo, privacy policy URL, homepage URL)
3. For the Gmail scope `gmail.send`, Google may require a security review — this typically takes 3–7 business days
4. Until approved, add a note in the README telling users to add themselves as a test user in your project, or instruct them to create their own Google Cloud project (which avoids this entirely — see 0.2)

**Better long-term fix (0.2):** Have users create their own Google Cloud project and drop in their own `credentials.json`. This completely sidesteps the verification issue and keeps costs and quotas separate per user. The README already documents this path.

### 0.2 E\*TRADE production API access
**Problem:** Sandbox keys are easy to get but show simulated data. Live data requires E\*TRADE to manually review and approve your application.

**Fix:** Document clearly in README that:
- Sandbox is available immediately
- Live access requires filling out E\*TRADE's production access request form
- Typical approval time: 1–2 business days
- The API is read-only for now (portfolio monitoring, not trading), which speeds up approval

### 0.3 Daily E\*TRADE re-authorization UX
**Problem:** E\*TRADE OAuth 1.0a tokens expire at midnight ET every day. Currently the user must manually open a browser, paste a PIN, and restart the script. This makes unattended scheduling fragile.

**Short-term fix:** Add a proper error notification — if the scheduler fires but the token is expired, send an email/push notification telling the user to re-authorize.

**Better fix (Phase 1.3):** See below.

---

## Phase 1 — Production Hardening

Make it reliable enough to run unattended on a home server or cloud VM.

### 1.1 Persistent background service
- Package as a `systemd` unit (Linux) and `launchd` plist (macOS) with sample config files
- Docker image so it can run on any VPS — users just `docker run` with their `.env`
- Health check endpoint (simple HTTP) so uptime monitoring tools can watch it

### 1.2 Graceful error handling + alerting
- If news scraping fails for a symbol, send the email anyway with a note
- If the LLM API is down or quota-exceeded, fall back to keyword analysis and note it in the email
- Add a simple retry queue for failed email sends
- Add a "heartbeat" email if neither scheduled email was sent that day

### 1.3 E\*TRADE daily auth — mobile-assisted flow
**Current UX:** Terminal prompt → user pastes PIN → cumbersome for headless servers.

**Option A — Mobile deep link (near-term):**
Send yourself a push notification (via Pushover, Ntfy, or Telegram bot) with the E\*TRADE auth URL as a tappable link. You authorize on your phone in 10 seconds. The desktop process polls a local file or SQLite for the PIN.

**Option B — Automated PIN extraction via SMS (advanced):**
E\*TRADE sends an SMS code. With a service like Twilio or a personal number you control, you can receive that SMS programmatically. This fully automates the flow but requires additional credentials setup.

**Option C — Selenium/browser automation (fragile):**
Headless Chrome logs in with your E\*TRADE credentials automatically. Works but brittle — breaks when E\*TRADE changes their UI and requires storing your brokerage password in plaintext. Not recommended for a public tool.

**Recommended path:** Option A. Document Option B as advanced.

### 1.4 Rate limiting + cost controls
- Add per-run LLM cost tracking to a local SQLite log
- Weekly cost summary email (Friday evening) showing spend breakdown
- Alert if single call exceeds $0.05 (catches prompt injection or runaway loops)

---

## Phase 2 — Better Intelligence

Make the AI analysis genuinely useful rather than generic.

### 2.1 Premium news sources
- **SEC EDGAR filings** — 8-K, 10-Q, earnings releases. More reliable signal than news headlines.
- **Earnings calendar** — flag positions reporting earnings this week; automatically adjust risk assessment
- **Reddit/StockTwits sentiment** — retail sentiment as a contrarian indicator
- **Options flow** — unusual options activity (via Unusual Whales or Tradier API) as a leading indicator

### 2.2 Better LLM prompting
- Include the previous day's recommendation in the prompt — LLM can track whether its call was right and update its view
- Add a "conviction score" (1–5) alongside each recommendation
- Ask the LLM to flag when its recommendation conflicts with the broader market trend
- System prompt persona: let users configure their risk tolerance (aggressive growth vs income/capital preservation)

### 2.3 Multi-account and multi-broker support
- Support multiple E\*TRADE accounts (taxable + IRA)
- Add Schwab API (launched 2024, replacing TD Ameritrade/thinkorswim)
- Add Robinhood via unofficial API (robin_stocks library)
- Aggregate positions across all accounts for unified analysis

### 2.4 Performance tracking
- After each pre-market call, log the predicted move vs actual close
- Weekly accuracy report: "Your HOLD on MSFT was right 4/5 times this week"
- Track AI model accuracy over time — switch models if one consistently outperforms

---

## Phase 3 — Quantitative Factors

Add systematic, rules-based signal generation on top of AI narrative.

### 3.1 Technical indicators
Compute at runtime using `pandas-ta` or `ta-lib`:
- **Momentum:** RSI, MACD signal line crossover
- **Trend:** 50/200-day moving average slope, golden/death cross detection
- **Volatility:** Bollinger Band width, ATR (for dynamic stop-loss sizing)
- **Volume:** OBV (On-Balance Volume), VWAP deviation

These feed into the LLM prompt as structured data points alongside news, producing more grounded recommendations.

### 3.2 Factor-based scoring model
Build a lightweight multi-factor score (0–100) for each position at each report:
- Momentum factor (recent price action)
- Quality factor (earnings beat/miss history)
- Sentiment factor (AI news score)
- Positioning factor (short interest, institutional ownership changes)

Display the factor breakdown in the email. This makes the recommendation transparent and auditable.

### 3.3 Backtesting framework
Before trusting any signal, backtest it:
- Use `backtrader` or `vectorbt` to replay signals on historical price data
- Test: "if I had followed every BUY recommendation this quarter, what would my return be?"
- Display rolling win rate and Sharpe ratio in the weekly performance email

---

## Phase 4 — Automated Trading

> ⚠️ This phase introduces real financial risk. Every component should be independently tested with paper trading before enabling live execution.

### 4.1 E\*TRADE order API (read → write)
E\*TRADE's production API supports order placement. Required steps:
- Request **Level 2 API access** from E\*TRADE (trading permissions, separate from read-only)
- All orders must go through an order preview → confirm two-step flow
- Start with paper trading via the sandbox `POST /v1/accounts/{key}/orders/preview`

### 4.2 Risk management layer (non-negotiable before live trading)
Before any order is placed, enforce:
- **Position size limit:** no single position > X% of portfolio (configurable)
- **Daily loss limit:** halt all activity if portfolio is down > Y% in one day
- **Sector concentration limit:** no single sector > Z%
- **Order size cap:** no single order > $N
- **Dry-run mode:** log what *would* be traded without sending orders (default on)

### 4.3 Signal-to-order pipeline
```
News + Technicals → AI recommendation → Factor score > threshold → Risk check → Order preview → Execute
```
Only execute if:
- AI recommendation = BUY/SELL
- Factor score agrees (same direction)
- Risk checks all pass
- Confidence > configurable threshold

### 4.4 Execution quality
- Use **limit orders** (never market orders for automated trading)
- Time-in-force: DAY — if not filled by close, cancel
- For sells, prefer selling covered calls first (if eligible) before liquidating
- Post-execution email confirmation for every order placed

---

## Phase 5 — SaaS / Multi-User (long-term)

If this grows beyond personal use:

### 5.1 Web dashboard
- Replace email-only output with a simple web UI (FastAPI + HTMX)
- Portfolio timeline chart, recommendation history, accuracy metrics
- User can override AI recommendation and the system learns from corrections

### 5.2 Authentication and multi-tenancy
- Each user has their own E\*TRADE + LLM credentials stored encrypted
- Per-user scheduling, risk settings, notification preferences
- Free tier (Gemini backend) vs Pro tier (Claude backend, more news sources)

### 5.3 Monetization options (if going public)
- **Freemium SaaS:** free for sandbox/Gemini, paid subscription unlocks live trading + premium news
- **Open-source + hosted:** keep code free, charge for the managed hosted version
- **API:** charge developers per analysis call

### 5.4 Compliance and legal considerations
- Investment advice regulations vary by jurisdiction — the tool should always disclaim it is not providing personalized investment advice
- If adding automated execution, consult whether this constitutes operating an investment advisor under SEC rules
- For any public SaaS, a privacy policy and terms of service are legally required

---

## Priority summary

| Priority | Item | Effort | Impact |
|---|---|---|---|
| 🔴 Must-do | Google OAuth production verification | Medium | Blocks all new users |
| 🔴 Must-do | E\*TRADE live API documentation | Low | Blocks real data |
| 🔴 Must-do | E\*TRADE daily auth mobile flow | Medium | Blocks unattended use |
| 🟡 High | Docker packaging + systemd service | Low | Reliability |
| 🟡 High | Performance tracking (call accuracy) | Medium | Trust in the system |
| 🟡 High | Technical indicators in prompt | Medium | Better analysis |
| 🟢 Medium | SEC EDGAR filings feed | Medium | Better signals |
| 🟢 Medium | Multi-account support | Medium | Broader appeal |
| 🟢 Medium | Backtesting framework | High | Pre-trading validation |
| ⚪ Later | E\*TRADE order execution | Very High | Real automation |
| ⚪ Later | Web dashboard | High | Scale beyond email |
| ⚪ Later | Multi-user SaaS | Very High | Productization |
