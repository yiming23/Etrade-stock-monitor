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

### [2026-04-16] Quant + LLM integration into daily report
- Quant runs first (step 5a), LLM runs second (step 5b) with quant signals as context
- LLM prompt now includes a `=== QUANT SIGNALS ===` section — LLM acts as PM synthesizing both news and medium-term factor signals
- Email layout: quant panel on top (reference), LLM recommendation below (incorporates quant in action_detail)
- LLM prompt includes today's date to fix incorrect earnings countdown calculations
- Fixed ETF 404 noise: `data/fetcher.py` now skips fundamentals calls for known ETFs (GLD, QQQ, VOO, etc.) and suppresses yfinance stderr output
- Fixed Wikipedia 403: `src/universe/sp500.py` now uses `requests` with a browser User-Agent instead of `pd.read_html()` directly
- Added `data/market_cache/` to `.gitignore` — rebuilt automatically by yfinance on first run

### [2026-04-16] IC analysis & walk-forward validation (local research run)
- Ran `--ic-analysis` on 503 S&P 500 stocks (2y history) → `data/ic_results.json`
- Ran `--walk-forward` OOS validation (train 12m, test 1m, roll forward)
- Updated `quant_model.json` to v1.1 with OOS-validated weights (see Quant Model section)

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
- [x] Run `--ic-analysis` on S&P 500 to get actual IC for each signal
- [x] Run `--walk-forward` for OOS validation
- [x] Update weights from IC results (`quant_model.json`)
- [x] Wire into daily report email
- [ ] After 3-6 months live: replace linear weights with Ridge/Logistic regression

---

### [2026-04-16] Backtest rigor overhaul — look-ahead bias fix + multi-regime support

**Problem found:** `backtest_visual.py` had pervasive look-ahead bias. `compute_signals()` was called
with the full fetcher (data up to today), so every historical index used TODAY's signal values,
not the signal values that existed at that historical date. This inflated Sharpe to 6.35 — artifact, not real alpha.
Fundamental signals (analyst_revision, short_interest, sue, insider_net) cannot be computed
point-in-time from yfinance (only current snapshots available).

**Changes:**

- **`src/research/backtest_visual.py`** — complete rewrite:
  - Only price-derivable signals: `momentum_12m_1m`, `momentum_1m`, `rel_strength`
  - Strict PIT computation: for each sample `idx`, uses `prices.iloc[idx - N]` — never future data
  - Pre-computed expanding mean/std for O(1) per sample vs previous O(n²)
  - PIT weights renormalized from v1.1: {mom_12m: 0.66, mom_1m: 0.23, rel_strength: 0.11}
  - `NAMED_PERIODS`: `recent`, `post_covid`, `covid`, `bull_2010s`, `gfc`, `full_cycle`, `dot_com`
  - SPY buy-and-hold baseline on P&L chart (3 lines: L/S, Long Q5, SPY)
  - SPY avg monthly return as dashed baseline on quintile spread chart
  - Chart title clearly states signals included and survivorship bias warning

- **`src/data/fetcher.py`** — backfill detection:
  - `get_prices()` checks if cached data covers requested start date
  - If not (cache has 2y, request 20y), triggers backfill fetch for missing historical window
  - `_fetch_prices()` accepts optional `end` parameter for bounded historical fetches

- **`src/research/run.py`** — new CLI args:
  - `--period {recent,post_covid,covid,bull_2010s,gfc,full_cycle,dot_com}`
  - `--start-date YYYY-MM-DD` / `--end-date YYYY-MM-DD` for custom ranges

**Signals excluded from backtest (cannot compute PIT from free data):**
- `analyst_revision`, `short_interest`, `sue`, `insider_net` — yfinance only provides current snapshots
- These remain in the live daily model; gap is a known limitation without Bloomberg/Compustat PIT DB

**Survivorship bias:** All periods use current S&P 500 constituents. Removed companies excluded
→ results biased upward, especially for GFC/dot-com. Noted in chart title.

### [2026-04-16] IC analysis + walk-forward: look-ahead bias fix + hybrid weighting

**Problem found:** Same look-ahead bias as backtest existed in IC analysis and walk-forward.
Both called `compute_signals(sym, fetcher)` with full data, then ignored the `hist = prices.iloc[:idx]`
they computed. Signal values were fixed at today's values for ALL historical sample dates.
The walk-forward OOS IC results (v1.1) were therefore not trustworthy.

**Changes:**

- **`src/research/ic_analysis.py`** — complete rewrite:
  - `_compute_signal_return_pairs()`: now uses pre-computed expanding mean/std, reads
    signals at `prices.iloc[idx - N]` for each sample index — strictly no future data
  - `walk_forward_validation()`: same PIT fix; only price signals validated
  - Price signals analyzed: `momentum_12m_1m`, `momentum_1m`, `rel_strength`
  - Fundamental signals (`analyst_revision`, `sue`, `short_interest`, `insider_net`, etc.)
    NOT computed empirically — yfinance has no historical snapshots
  - Added `_ACADEMIC_IC`: literature IC values from peer-reviewed papers, used as
    permanent weights for fundamental signals
  - `_ic_to_weights()`: hybrid approach — PIT IC for price signals, academic IC for fundamentals
  - Output now labels each signal as `pit_empirical` vs `academic_prior`

- **`src/forecast/quant.py`** — `_PRIOR_WEIGHTS` updated:
  - Now derived from academic IC values (normalized to sum 1.0)
  - Consistent with `_ACADEMIC_IC` in ic_analysis.py
  - Each weight documented with source paper and approximate IC

- **`data/quant_model.json`** — reset to v2.0-hybrid-academic-prior:
  - v1.1 (biased OOS weights) discarded
  - Academic IC-derived weights as new baseline
  - Full notes on methodology and data sources

**Hybrid weighting rationale:**
  - Price signals have clean PIT history → use empirical IC (updated by --ic-analysis)
  - Fundamental signals lack PIT data → use academic IC as permanent prior
  - Academic IC comes from Bloomberg/Compustat studies (Jegadeesh, Bernard, Dechow, etc.)
  - This is the industry-standard approach for systematic funds without a PIT database

**Weight update workflow (after fix):**
  1. Run `--ic-analysis` → computes PIT IC for price signals
  2. Hybrid function blends PIT IC (price) + academic IC (fundamentals)
  3. Writes to `quant_model.json` → live model picks up on next restart

### [2026-04-16] Phase 1 new signals: high_52w_ratio + beta_12m

**Rationale:** Newer literature (post-2000) validates two additional price signals that are strictly
computable point-in-time from free price data, with no new data dependencies:

- **`high_52w_ratio`** (George & Hwang 2004): `close / rolling_252d_max`. Stocks near their
  52-week high tend to continue outperforming — the high acts as a momentum anchor. IC ≈ 0.045.
  McLean & Pontiff (2016) found 52-week high factor decayed less post-publication than most factors.

- **`beta_12m`** (Frazzini & Pedersen 2014 BAB): rolling 12-month beta vs SPY, **inverted**.
  Low-beta stocks earn excess risk-adjusted returns (betting against beta premium). IC ≈ 0.035.
  Signal: `beta_12m = -normalized(rolling_beta)` so low beta → positive (bullish).

**Files changed:**
- `src/forecast/signals.py` — added to `SignalBundle` dataclass + `compute_signals()`. total=13.
- `src/research/backtest_visual.py` — PIT computation in `_collect_records()` using pre-computed
  expanding stats + rolling beta vs SPY. `_PIT_WEIGHTS` updated to 5 signals (academic IC normalized).
  `_collect_records()` now accepts `spy_prices` parameter.
- `src/research/ic_analysis.py` — added to `_PRICE_SIGNALS` frozenset + `_compute_signal_return_pairs()`
  + `walk_forward_validation()`. SPY fetched inside each function.
- `src/forecast/quant.py` — `_PRIOR_WEIGHTS` updated to 13 signals. Total raw IC = 0.398.
  `signal_descriptions` updated in `_build_narrative`.
- `data/quant_model.json` — updated to v2.1-hybrid-academic-prior (13 signals).

**PIT weights (backtest, 5 price signals, academic IC normalized to 1.0):**

| Signal | Academic IC | PIT Weight |
|--------|------------|------------|
| momentum_12m_1m | 0.060 | 0.316 |
| high_52w_ratio | 0.045 | 0.237 |
| rel_strength | 0.040 | 0.211 |
| beta_12m | 0.035 | 0.184 |
| momentum_1m | 0.010 | 0.053 |

**Phase 2 (deferred):** `gross_profitability`, `asset_growth`, `roe` from yfinance financials.
These require testing yfinance financial statement coverage and cannot be PIT-backtested.

---

## Next Up

### Backtest debug (pending investigation)
- [ ] **Why does SPY look so high in long-run cumulative chart?**
  Hypothesis A: survivorship bias inflates all returns in long periods (GFC/full_cycle).
  Hypothesis B: SPY cumulative uses 21-trading-day forward return per rebalancing date
  (i.e. compounding ~12 returns/year), which can diverge from true buy-and-hold if dates
  aren't exactly calendar-monthly. Need to verify `_compute_spy_monthly()` alignment.
  Hypothesis C: for long periods (22y), compounding of even small positive monthly drifts
  produces very large terminal values — may be correct, just looks surprising.
  **Action:** print raw SPY monthly returns from `_compute_spy_monthly()`, compare to
  actual SPY annualized CAGR for the same period.

- [ ] **Why does the model underperform the market (L/S or Long Q5 < SPY)?**
  Likely causes:
  1. Survivorship bias helps SPY baseline equally — not the root cause.
  2. Price-only signals (momentum, beta, 52w-high) are weakest in pure bull markets;
     they shine in volatile/mean-reverting regimes (GFC, COVID). Check GFC period.
  3. Monthly rebalancing + 21d forward return may be too short for momentum signals
     (optimal holding is 3-12 months for Jegadeesh & Titman).
  4. Signal weights are academic priors, not calibrated to this universe — run
     `--ic-analysis` first, then re-run backtest with updated weights.
  **Action:** compare recent vs gfc periods; if model beats SPY in GFC but not recent,
  it's regime-dependent (expected). If underperforms everywhere → weight calibration issue.

### Model iteration
- [ ] Run `./run_backtests.sh --portfolio ANET,COIN` locally (recent + gfc + full_cycle)
- [ ] Run `--ic-analysis` to calibrate price signal weights from empirical PIT IC
- [ ] Re-run backtest after IC update — compare performance
- [ ] Weekly screener run → email top 5 S&P 500 opportunities
- [ ] Accumulate live backtest data (LLM + quant predictions vs actuals)
- [ ] Phase 2 model: Ridge/Logistic regression once 3-6 months of data exists
- [ ] Re-run `--ic-analysis` + `--walk-forward` every 1-2 months to refresh weights
- [ ] Phase 2 signals (deferred): `gross_profitability`, `asset_growth`, `roe` from
      yfinance financials — need to test coverage/reliability first
- [ ] Long-term: evaluate Compustat/WRDS for point-in-time fundamental data
