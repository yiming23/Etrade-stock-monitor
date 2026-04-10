"""
E*TRADE portfolio/positions reader.

Fetches account list and portfolio positions via the pyetrade Accounts API.
"""

from dataclasses import dataclass, field

import pyetrade

from src.utils.config import Settings
from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class Position:
    """A single stock position."""

    symbol: str
    description: str
    quantity: float
    cost_basis: float         # total cost basis (cost_per_share * quantity)
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

    @property
    def symbols(self) -> list[str]:
        """Get list of all stock symbols."""
        return [p.symbol for p in self.positions]


class PortfolioReader:
    """Reads portfolio data from E*TRADE."""

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
        """
        Fetch portfolio for the specified account (default: first account).

        Returns a PortfolioSummary with all positions.
        """
        # Step 1: List accounts
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

        logger.info(f"Fetching portfolio for account: {account_name} ({account_id_key})")

        # Step 2: Get portfolio positions
        try:
            portfolio_data = self.accounts_api.get_account_portfolio(
                account_id_key,
                resp_format="json",
            )
        except Exception as e:
            logger.warning(f"Failed to fetch portfolio: {e}")
            return PortfolioSummary(
                account_id=account_id_key,
                account_name=account_name,
            )

        # Step 3: Parse positions
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

                # P&L from API — E*TRADE may return these under Performance
                # or directly on the position object. Try all known field names.
                api_total_gain = (
                    float(perf.get("totalGain", 0))
                    or float(perf.get("gain", 0))
                    or float(pos.get("totalGain", 0))
                    or float(pos.get("unrealizedGain", 0))
                )
                api_total_gain_pct = (
                    float(perf.get("totalGainPct", 0))
                    or float(perf.get("gainPct", 0))
                    or float(pos.get("totalGainPct", 0))
                    or float(pos.get("unrealizedGainPct", 0))
                )

                # If API gave us nothing (sandbox or field missing), compute
                # from cost-per-share vs current price — more reliable.
                if api_total_gain == 0 and cps > 0 and qty > 0:
                    total_gain = (current_price - cps) * qty
                    total_gain_pct = (current_price - cps) / cps * 100
                else:
                    total_gain = api_total_gain
                    total_gain_pct = api_total_gain_pct

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
        logger.info(f"Found {len(positions)} positions, total value: ${total_value:,.2f}")

        return PortfolioSummary(
            account_id=account_id_key,
            account_name=account_name,
            positions=positions,
            total_market_value=total_value,
        )
