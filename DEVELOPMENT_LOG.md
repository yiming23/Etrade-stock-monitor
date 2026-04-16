# E*TRADE Stock Monitor — Development Log

> Shared context file. Start each new session by reading this, then append new entries at the bottom.

---

## Standards We Follow

- **Strangler Fig Pattern** — extract modules incrementally, never big-bang rewrite
- **SRP** — each module has one job
- **OCP** — extend via new classes, don't modify existing ones (e.g. `Forecaster` interface)
- **Bounded Context** — `backtest/`, `forecast/`, `email/` know nothing about each other

---

## Log

### [2026-03] Initial build
- E*TRADE OAuth + portfolio fetch
- LLM analysis → HTML email report
- APScheduler (3 daily jobs) + macOS launchd LaunchAgent
- Telegram bot for remote control + re-auth PIN flow

### [2026-04-13] Reliability fixes
- Mac sleep was causing APScheduler to silently drop jobs (misfire)
- Fix: raised `misfire_grace_time`, added startup catch-up, bridged APScheduler logs to loguru, added pmset wake schedule

### [2026-04-13] Prediction tracking & backtest
- SQLite DB tracking LLM predictions (direction + magnitude) and yfinance actuals
- Backtest engine + Markdown report export
- Email now shows per-stock historical accuracy badges (after ≥3 samples)
- `--dry-run` flag for testing without polluting the DB

### [2026-04-13] System design scaffolding
- Created `src/forecast/base.py` — abstract `Forecaster` interface (OCP foundation for future ML)
- Extracted `src/backtest/` as a bounded context

### [2026-04-16] Migrated to DigitalOcean cloud server
- Mac sleep was fundamentally unreliable — jobs kept missing even with 2h misfire grace
- Migrated to DigitalOcean $6/month Ubuntu droplet (never sleeps, always on)
- Added `stockmonitor.service` (systemd) to replace macOS launchd
- Added `restart_server.sh` (Linux equivalent of `restart.sh`)
- Added `setup_server.sh` (one-time server bootstrap script)
- Repo made public so server can `git clone` without auth
- Deploy workflow: edit on Mac → `git push` → `git pull && ./restart_server.sh` on server
- Credentials (`.env`, `credentials.json`, `gmail_token.json`) copied via `scp`, never in git

### [2026-04-15] Wired QuantForecaster into daily report pipeline
- `main.py`: Added step 5b — runs `QuantForecaster.forecast()` on all portfolio positions after LLM step
- `email/sender.py`: Added quant block per stock (light-blue panel, "Quant (medium-term)" label)
  - Shows direction arrow, estimated move %, recommendation badge, confidence level, narrative
  - LLM row now labeled "LLM:" to distinguish short-term (news) vs medium-term (signals)
- Quant is fully non-fatal: if signals fail, email still sends with LLM results only

### [2026-04-16] Quant forecasting system (scaffolding complete)
- Built `src/universe/sp500.py` — S&P 500 universe with sector → ETF mapping, weekly cached
- Built `src/data/fetcher.py` — batch historical data fetcher, Parquet cache, options snapshot, analyst/insider data
- Built `src/forecast/signals.py` — 11 quant signals across 3 tiers (see Quant Model section below)
- Built `src/forecast/quant.py` — `QuantForecaster(Forecaster)`, IC-weighted composite score, outputs direction + magnitude + position recommendation
- Built `src/research/ic_analysis.py` — IC/IC-IR computation, walk-forward OOS validation, weight update
- Built `src/research/screener.py` — full S&P 500 screen, ranked output, sector/direction filters
- Built `src/research/run.py` — CLI: `python -m src.research.run --ic-analysis / --screen / --walk-forward`
- Research pipeline is fully separate from daily report; daily report will use same `QuantForecaster`
- Pending: wire `QuantForecaster` into `main.py` daily report + email display

---

## Quant Model Development Log

### v1.1-oos-validated [2026-04-16] — Walk-forward OOS validation on 503 S&P 500 stocks

**Key findings vs v1.0 priors:**
- `momentum_12m_1m`: OOS IC +0.21 — strongest signal, weight raised to 0.35
- `iv_rank`: **FLIPPED** — in-sample +0.14 but OOS -0.21 (look-ahead bias in in-sample test). Excluded.
- `short_interest`: OOS IC +0.08 — actually stronger OOS than in-sample, weight raised
- `momentum_1m`: OOS IC +0.07 — much stronger than prior suggested (was 0.01), raised to 0.12
- `put_call_ratio`, `iv_skew`, `volume_surge`: Excluded — negative or insignificant OOS IC
- `insider_net`: No yfinance coverage for IC computation, kept at small prior weight (0.08)

**OOS weights (v1.1):**

| Signal | OOS IC | Weight |
|--------|--------|--------|
| momentum_12m_1m | +0.21 | 0.35 |
| analyst_revision | +0.09 | 0.15 |
| short_interest | +0.08 | 0.14 |
| momentum_1m | +0.07 | 0.12 |
| sue | +0.06 | 0.10 |
| insider_net | (prior) | 0.08 |
| rel_strength | +0.04 | 0.06 |
| iv_rank / iv_skew / put_call_ratio / volume_surge | ≤0 | 0.00 |

### v1.0-prior [2026-04-16] — Academic prior weights, not yet validated

**Design**
- IC-weighted linear combination of 11 signals
- Time-series z-score normalization per signal (avoids cross-sectional ranking issues with small universe)
- Magnitude scaled by stock's own historical volatility (20-day holding period assumption)
- Weights stored in `data/quant_model.json`, updated by research pipeline

**Signals (3 tiers)**

| Signal | Tier | Weight | Reference |
|--------|------|--------|-----------|
| analyst_revision | 1 | 0.22 | Chan, Jegadeesh & Lakonishok (1996) |
| sue (PEAD) | 1 | 0.20 | Bernard & Thomas (1989) |
| momentum_12m_1m | 1 | 0.18 | Jegadeesh & Titman (1993) |
| rel_strength | 2 | 0.12 | Moskowitz & Grinblatt (1999) |
| insider_net | 2 | 0.10 | Seyhun (1986) |
| short_interest | 2 | 0.08 | Dechow et al (2001) |
| put_call_ratio | 3 | 0.04 | Pan & Poteshman (2006) |
| iv_skew | 3 | 0.03 | Xing, Zhang & Zhao (2010) |
| iv_rank | 3 | 0.01 | context only |
| momentum_1m | 3 | 0.01 | weak, reversal risk |
| volume_surge | 3 | 0.01 | Gervais et al (2001) |

**Next steps for model iteration**
- [ ] Run `--ic-analysis` on S&P 500 to get actual IC for each signal
- [ ] Run `--walk-forward` for OOS validation
- [ ] Update weights from IC results (`quant_model.json`)
- [ ] Wire into daily report email
- [ ] After 3-6 months live: replace linear weights with Ridge/Logistic regression

---

## Next Up

- [ ] Run `--ic-analysis` on S&P 500 to validate signals with real historical data
- [ ] Run `--walk-forward` OOS validation, update `quant_model.json` weights
- [ ] Weekly screener run → email top 5 S&P 500 opportunities
- [ ] Accumulate live backtest data (LLM + quant predictions vs actuals)
- [ ] Phase 2 model: Ridge/Logistic regression once 3-6 months of data exists
