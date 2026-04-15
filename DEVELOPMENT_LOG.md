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

---

## Next Up

- [ ] Accumulate backtest data (need ~3–4 weeks before numbers are meaningful)
- [ ] Wrap `StockAnalyzer` as `LLMForecaster(Forecaster)` — no behavior change, just wires the interface
- [ ] `QuantForecaster` — technical indicators (RSI, MACD, earnings surprise history)
- [ ] Ensemble/calibration layer — weight LLM vs quant based on historical accuracy
- [ ] ML forecaster (longer term, once enough backtest data exists)
