"""
E*TRADE portfolio/positions reader with local cache + yfinance enrichment.

Two modes:
  LIVE   — full E*TRADE API call (needs OAuth, once per PORTFOLIO_CACHE_DAYS)
  CACHED — load symbol/qty/cost from JSON, enrich with live yfinance prices
           (runs every time, no E*TRADE auth required)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pyetrade

from src.utils.config import Settings, PROJECT_ROOT
from src.utils.logger import get_logger

logger = get_logger(__name__)

PORTFOLIO_CACHE_FILE = PROJECT_ROOT / ".portfolio_cache.json"


@dataclass
class Position:
    """A single stock position."""
    symbol: str
    description: str
    quantity: float
    cost_basis: float         # total cost (cost_per_share × quantity)
    cost_per_share: float     # average cost per share
    market_value: float
    current_price: float
    day_change: float
    day_change_pct: float
    total_gain: float
    total_gain_pct: float


@dataclass
class PortfolioSummary:
    """Summary of all positions in the account."""
    account_id: str
    account_name: str
    positions: list[Position] = field(default_factory=list)
    total_market_value: float = 0.0
    from_cache: bool = False      # True when prices came from yfinance, not E*TRADE

    @property
    def symbols(self) -> list[str]:
        return [p.symbol for p in self.positions]


# =============================================================================
# Cache helpers
# =============================================================================

def save_portfolio_cache(portfolio: PortfolioSummary) -> None:
    """Persist symbol/quantity/cost to disk. Called after a live E*TRADE fetch."""
    data = {
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "account_id": portfolio.account_id,
        "account_name": portfolio.account_name,
        "positions": [
            {
                "symbol": p.symbol,
                "description": p.description,
                "quantity": p.quantity,
                "cost_per_share": p.cost_per_share,
            }
            for p in portfolio.positions
        ],
    }
    PORTFOLIO_CACHE_FILE.write_text(json.dumps(data, indent=2))
    logger.info(
        f"Portfolio cache saved ({len(portfolio.positions)} positions)."
    )


def load_portfolio_from_cache(settings: Settings) -> PortfolioSummary | None:
    """
    Load positions from cache and enrich with live yfinance prices.
    Returns None if cache doesn't exist or is older than portfolio_cache_days.
    """
    if not PORTFOLIO_CACHE_FILE.exists():
        return None

    try:
        data = json.loads(PORTFOLIO_CACHE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    # Check age
    cache_days = settings.portfolio_cache_days
    if cache_days > 0:
        try:
            cached_at = datetime.fromisoformat(data["cached_at"])
            age_days = (datetime.now(timezone.utc) - cached_at).total_seconds() / 86400
            if age_days > cache_days:
                logger.info(
                    f"Portfolio cache is {age_days:.1f} days old "
                    f"(limit: {cache_days}). Will re-auth with E*TRADE."
                )
                return None
        except (KeyError, ValueError):
            return None

    raw_positions = data.get("positions", [])
    if not raw_positions:
        return None

    symbols = [p["symbol"] for p in raw_positions]
    logger.info(
        f"Loading portfolio from cache ({len(symbols)} symbols). "
        f"Enriching prices via yfinance..."
    )

    live_prices = _fetch_live_prices(symbols)

    positions: list[Position] = []
    for p in raw_positions:
        sym = p["symbol"]
        qty = float(p.get("quantity", 0))
        cps = float(p.get("cost_per_share", 0))
        price_data = live_prices.get(sym, {})

        current_price = price_data.get("price", 0.0)
        day_change = price_data.get("day_change", 0.0)
        day_change_pct = price_data.get("day_change_pct", 0.0)
        market_value = current_price * qty
        total_gain = (current_price - cps) * qty if cps else 0.0
        total_gain_pct = ((current_price - cps) / cps * 100) if cps else 0.0

        positions.append(Position(
            symbol=sym,
            description=p.get("description", sym),
            quantity=qty,
            cost_basis=cps * qty,
            cost_per_share=cps,
            market_value=market_value,
            current_price=current_price,
            day_change=day_change,
            day_change_pct=day_change_pct,
            total_gain=total_gain,
            total_gain_pct=total_gain_pct,
        ))

    total_value = sum(p.market_value for p in positions)
    logger.info(
        f"Cached portfolio loaded. {len(positions)} positions, "
        f"total value ${total_value:,.2f} (live prices via yfinance)."
    )

    return PortfolioSummary(
        account_id=data.get("account_id", "cached"),
        account_name=data.get("account_name", "Cached Portfolio"),
        positions=positions,
        total_market_value=total_value,
        from_cache=True,
    )


def _fetch_live_prices(symbols: list[str]) -> dict[str, dict]:
    """
    Fetch current price + day change for a list of symbols via yfinance.
    Returns dict: {symbol: {price, day_change, day_change_pct}}
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance not installed — prices will be 0. Run: pip install yfinance")
        return {}

    result: dict[str, dict] = {}
    try:
        tickers = yf.Tickers(" ".join(symbols))
        for sym in symbols:
            try:
                info = tickers.tickers[sym].fast_info
                price = float(getattr(info, "last_price", 0) or 0)
                prev_close = float(getattr(info, "previous_close", 0) or 0)
                day_change = price - prev_close if prev_close else 0.0
                day_change_pct = (day_change / prev_close * 100) if prev_close else 0.0
                result[sym] = {
                    "price": price,
                    "day_change": day_change,
                    "day_change_pct": day_change_pct,
                }
                logger.debug(f"  {sym}: ${price:.2f} ({day_change_pct:+.2f}%)")
            except Exception as e:
                logger.warning(f"yfinance price fetch failed for {sym}: {e}")
                result[sym] = {"price": 0.0, "day_change": 0.0, "day_change_pct": 0.0}
    except Exception as e:
        logger.error(f"yfinance batch fetch failed: {e}")

    return result


# =============================================================================
# Earnings calendar helper (used by post-market analyzer)
# =============================================================================

def fetch_earnings_calendar(symbols: list[str]) -> dict[str, dict]:
    """
    Fetch upcoming earnings dates for a list of symbols via yfinance.
    Returns dict: {symbol: {earnings_date, eps_estimate, revenue_estimate}}
    """
    try:
        import yfinance as yf
    except ImportError:
        return {}

    result: dict[str, dict] = {}
    for sym in symbols:
        try:
            ticker = yf.Ticker(sym)
            cal = ticker.calendar
            if cal is None:
                continue
            # yfinance returns a dict or DataFrame depending on version
            if hasattr(cal, "to_dict"):
                cal = cal.to_dict()
            earnings_date = None
            if "Earnings Date" in cal:
                ed = cal["Earnings Date"]
                if isinstance(ed, list) and ed:
                    earnings_date = str(ed[0])
                else:
                    earnings_date = str(ed)
            if earnings_date:
                result[sym] = {
                    "earnings_date": earnings_date,
                    "eps_estimate": cal.get("EPS Estimate", "N/A"),
                    "revenue_estimate": cal.get("Revenue Estimate", "N/A"),
                }
        except Exception as e:
            logger.debug(f"Earnings calendar fetch failed for {sym}: {e}")

    return result


# =============================================================================
# Live E*TRADE portfolio reader
# =============================================================================

class PortfolioReader:
    """Reads portfolio data directly from E*TRADE API."""

    def __init__(
        self,
        settings: Settings,
        access_token: str,
        access_token_secret: str,
    ) -> None:
        self.settings = settings
        self.dev = settings.etrade_environment == "sandbox"
        self.accounts_api = pyetrade.ETradeAccounts(
            settings.etrade_consumer_key,
            settings.etrade_consumer_secret,
            access_token,
            access_token_secret,
            dev=self.dev,
        )

    def get_portfolio(self, account_index: int = 0) -> PortfolioSummary:
        """Fetch portfolio from E*TRADE and save to cache."""
        accounts = self.accounts_api.list_accounts(resp_format="json")
        account_list = accounts["AccountListResponse"]["Accounts"]["Account"]

        if account_index >= len(account_list):
            raise ValueError(
                f"Account index {account_index} out of range. "
                f"Found {len(account_list)} accounts."
            )

        account = account_list[account_index]
        account_id_key = account["accountIdKey"]
        account_name = account.get("accountName", account.get("accountDesc", "N/A"))
        logger.info(f"Fetching E*TRADE portfolio: {account_name} ({account_id_key})")

        try:
            portfolio_data = self.accounts_api.get_account_portfolio(
                account_id_key, resp_format="json"
            )
        except Exception as e:
            logger.warning(f"Failed to fetch portfolio: {e}")
            return PortfolioSummary(account_id=account_id_key, account_name=account_name)

        positions = []
        portfolio_response = portfolio_data.get("PortfolioResponse", {})
        account_portfolios = portfolio_response.get("AccountPortfolio", [])
        if not isinstance(account_portfolios, list):
            account_portfolios = [account_portfolios]

        for acct_portfolio in account_portfolios:
            pos_list = acct_portfolio.get("Position", [])
            if not isinstance(pos_list, list):
                pos_list = [pos_list]

            for pos in pos_list:
                product = pos.get("Product", {})
                quick = pos.get("Quick", {})
                perf = pos.get("Performance", {})

                qty = float(pos.get("quantity", 0))
                cps = float(pos.get("costPerShare", 0))
                market_value = float(pos.get("marketValue", 0))
                current_price = float(quick.get("lastTrade", 0))

                api_total_gain = (
                    float(perf.get("totalGain", 0))
                    or float(perf.get("gain", 0))
                    or float(pos.get("totalGain", 0))
                    or float(pos.get("unrealizedGain", 0))
                )
                if api_total_gain == 0 and cps > 0 and qty > 0:
                    total_gain = (current_price - cps) * qty
                    total_gain_pct = (current_price - cps) / cps * 100
                else:
                    total_gain = api_total_gain
                    total_gain_pct = (
                        float(perf.get("totalGainPct", 0))
                        or float(pos.get("totalGainPct", 0))
                        or ((current_price - cps) / cps * 100 if cps else 0)
                    )

                position = Position(
                    symbol=product.get("symbol", "UNKNOWN"),
                    description=pos.get("symbolDescription", product.get("symbol", "")),
                    quantity=qty,
                    cost_basis=cps * qty,
                    cost_per_share=cps,
                    market_value=market_value,
                    current_price=current_price,
                    day_change=float(quick.get("change", 0)),
                    day_change_pct=float(quick.get("changePct", 0)),
                    total_gain=total_gain,
                    total_gain_pct=total_gain_pct,
                )
                positions.append(position)
                logger.debug(
                    f"  {position.symbol}: {position.quantity:.0f} sh @ "
                    f"${position.current_price:.2f}, cost ${cps:.2f}, "
                    f"P&L {total_gain_pct:+.1f}%"
                )

        total_value = sum(p.market_value for p in positions)
        logger.info(
            f"E*TRADE: {len(positions)} positions, total ${total_value:,.2f}"
        )

        summary = PortfolioSummary(
            account_id=account_id_key,
            account_name=account_name,
            positions=positions,
            total_market_value=total_value,
            from_cache=False,
        )

        # Always save to cache after a successful live fetch
        save_portfolio_cache(summary)
        return summary
