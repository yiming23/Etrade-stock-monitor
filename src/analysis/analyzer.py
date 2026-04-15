"""
AI-powered portfolio analysis — PM-level per-stock calls.

Sends the top cross-portfolio news to the LLM in one call and gets back:
  - Per-stock: net sentiment, estimated price move, trend narrative,
    BUY/SELL/HOLD/TRIM recommendation with specific action detail
  - Overall market summary
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date

from src.news.scraper import NewsArticle
from src.utils.config import Settings
from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ArticleAnalysis:
    """Analysis for a single news article (used in email news section)."""
    rank: int
    symbol: str
    title: str
    url: str
    source: str
    published_str: str
    sentiment: str        # "bullish" | "bearish" | "neutral"
    impact_summary: str   # one sentence
    portfolio_pct: float  # what % of portfolio this stock represents


@dataclass
class StockCall:
    """PM-level per-stock recommendation."""
    symbol: str
    net_sentiment: str          # "bullish" | "bearish" | "neutral"
    estimated_move: str         # e.g. "+1.5% to +3%", "-2% to -4%"
    trend_narrative: str        # e.g. "gap up open, may consolidate midday"
    recommendation: str         # "BUY" | "SELL" | "HOLD" | "TRIM" | "ADD"
    action_detail: str          # specific advice: position size, entry, rationale
    stop_loss: str              # e.g. "$145.00" or "N/A"
    price_target_short: str     # e.g. "$162.00 (1-week)"
    # Structured prediction fields (used by backtest tracker)
    predicted_direction: str = "flat"       # "up" | "down" | "flat"
    predicted_magnitude_pct: float = 0.0   # midpoint of expected % move
    current_price: float = 0.0
    cost_basis: float = 0.0
    unrealized_pct: float = 0.0


@dataclass
class PortfolioAnalysis:
    """Full analysis result for the portfolio digest."""
    articles: list[ArticleAnalysis] = field(default_factory=list)
    stock_calls: list[StockCall] = field(default_factory=list)
    overall_summary: str = ""   # 1-2 sentence overall market read
    macro_note: str = ""        # key macro risk/tailwind for the portfolio


# --------------------------------------------------------------------------
# Prompts — one per report type
# --------------------------------------------------------------------------

_PROMPT_BASE = """\
You are a seasoned buy-side portfolio manager with 20+ years of experience.

=== PORTFOLIO HOLDINGS ===
{positions_text}

=== NEWS ARTICLES ===
{news_text}

{extra_context}
=== FOCUS ===
{focus_instructions}

=== OUTPUT FORMAT ===
Respond in this exact JSON format (no markdown, no extra text):
{{
  "overall_summary": "{summary_instruction}",
  "macro_note": "1 sentence: the single most important macro risk or tailwind for these holdings right now",
  "articles": [
    {{
      "rank": 1,
      "symbol": "TICKER or MACRO",
      "sentiment": "bullish" or "bearish" or "neutral",
      "impact_summary": "One precise sentence on what this news means for the stock or market"
    }}
  ],
  "stock_calls": [
    {{
      "symbol": "TICKER",
      "net_sentiment": "bullish" or "bearish" or "neutral",
      "predicted_direction": "up" or "down" or "flat",
      "predicted_magnitude_pct": 2.5,
      "estimated_move": "+1.5% to +3% today",
      "trend_narrative": "{narrative_instruction}",
      "recommendation": "BUY" or "ADD" or "HOLD" or "TRIM" or "SELL",
      "action_detail": "Specific advice referencing the holding's cost basis and current P&L",
      "stop_loss": "$000.00 or N/A",
      "price_target_short": "$000.00 (timeframe)"
    }}
  ]
}}

IMPORTANT: Include a stock_call for EVERY symbol in the portfolio holdings list.
"""

# 8:30 AM — Pre-market: react to overnight news, set opening trade plan
PRE_MARKET_FOCUS = """\
This is the PRE-MARKET brief (8:30 AM ET). Market opens in 1 hour.
1. Identify the key overnight / early-morning catalysts that will move stocks at open.
2. For each stock: predict the opening gap direction and first-hour price action.
3. Give actionable BUY/SELL/HOLD/TRIM calls specifically for the OPEN (first 30-60 min).
4. Flag any earnings, economic data, or Fed speakers due TODAY that could cause intraday reversals."""

PRE_MARKET_SUMMARY = "2 sentences: overall overnight sentiment and the #1 catalyst to watch at today's open"
PRE_MARKET_NARRATIVE = "Opening gap direction, likely first-hour move, key intraday catalysts today"

# 12:00 PM — Mid-day: summarize morning, update calls
MID_MARKET_FOCUS = """\
This is the MID-DAY brief (12:00 PM ET). Market has been open ~3.5 hours.
1. Summarize what actually happened since the open — which morning calls were right/wrong.
2. Update each position's recommendation based on morning price action and any new news.
3. Flag any afternoon catalysts (Fed speakers, economic releases, earnings after-close).
4. Identify positions that need attention (unusual volume, approaching stop levels, breakouts)."""

MID_MARKET_SUMMARY = "2 sentences: how the morning session went and what changed from the pre-market thesis"
MID_MARKET_NARRATIVE = "What happened this morning, updated stance for the afternoon session"

# 4:30 PM — Post-market: look ahead to tomorrow and next week
POST_MARKET_FOCUS = """\
This is the POST-MARKET brief (4:30 PM ET). Market just closed.
1. Summarize today's session and what it means for each position going forward.
2. Look AHEAD: what are the key upcoming events for each holding in the next 1-2 weeks?
   - Earnings dates (provided in extra context if available)
   - Macro events: Fed meetings, CPI/PPI releases, jobs reports, GDP
   - Geopolitical developments: trade war, tariff decisions, central bank decisions abroad, conflicts
3. For any stock with upcoming earnings, give a pre-earnings recommendation:
   - Should the user hold through earnings, trim beforehand, or add?
   - Based on current setup (valuation, momentum, cost basis), what is the expected move?
4. Rate each upcoming event as HIGH / MEDIUM / LOW impact for this specific portfolio."""

POST_MARKET_SUMMARY = "2 sentences: today's session result and the single most important event to watch in the coming week"
POST_MARKET_NARRATIVE = "Today's closing pattern + what to watch overnight and tomorrow morning"


class StockAnalyzer:
    """Analyzes portfolio news with a single LLM API call."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._daily_spent = 0.0
        self._last_reset_date = ""
        self._client = None

        backend = settings.llm_backend.lower()
        if backend == "claude":
            self._init_claude()
        elif backend == "gemini":
            self._init_gemini()
        elif backend == "fallback":
            logger.info("Using keyword-based fallback (no LLM API).")
        else:
            logger.warning(f"Unknown LLM_BACKEND '{backend}'. Using fallback.")

    def _init_claude(self) -> None:
        try:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
            self._backend = "claude"
            logger.info(f"Claude backend ready (model: {self.settings.claude_model}, "
                        f"daily cap: ${self.settings.daily_spend_limit_usd:.2f})")
        except Exception as e:
            logger.error(f"Claude init failed: {e}. Using fallback.")
            self._backend = "fallback"

    def _init_gemini(self) -> None:
        try:
            from google import genai
            from google.genai import types as genai_types
            self._gemini_client = genai.Client(api_key=self.settings.gemini_api_key)
            self._gemini_config = genai_types.GenerateContentConfig(
                max_output_tokens=4096,
                temperature=0.3,
                response_mime_type="application/json",
            )
            self._backend = "gemini"
            logger.info(f"Gemini backend ready (model: {self.settings.gemini_model})")
        except Exception as e:
            logger.error(f"Gemini init failed: {e}. Using fallback.")
            self._backend = "fallback"

    # -------------------------------------------------------------------------
    # Main entry point
    # -------------------------------------------------------------------------
    def analyze_top_news(
        self,
        top_articles: list[NewsArticle],
        positions: list,
        report_type: str = "pre_market",
        earnings_data: dict | None = None,
    ) -> PortfolioAnalysis:
        """
        Analyze top cross-portfolio articles + generate per-stock PM calls.

        Args:
            top_articles: ranked list from NewsScraper.select_top_portfolio_news()
            positions: list of Position objects
        """
        if not top_articles:
            return PortfolioAnalysis(overall_summary="No news articles available.")

        total_value = sum(p.market_value for p in positions) if positions else 0
        # Aggregate duplicate symbols
        value_by_symbol: dict[str, float] = {}
        for p in positions:
            value_by_symbol[p.symbol] = value_by_symbol.get(p.symbol, 0) + p.market_value
        weight_map = {
            sym: val / total_value * 100 if total_value else 0
            for sym, val in value_by_symbol.items()
        }

        # Build positions text — deduplicated, sorted by value
        seen_syms: set[str] = set()
        sorted_positions = sorted(positions, key=lambda x: x.market_value, reverse=True)
        positions_lines = []
        for p in sorted_positions:
            if p.symbol in seen_syms:
                continue
            seen_syms.add(p.symbol)
            cps = getattr(p, "cost_per_share", None) or (
                (p.cost_basis / p.quantity) if p.quantity else 0)
            unrealized_pct = ((p.current_price - cps) / cps * 100) if cps else 0
            positions_lines.append(
                f"  {p.symbol}: {p.quantity:.0f} shares @ ${p.current_price:.2f} | "
                f"cost basis ${cps:.2f}/share | "
                f"unrealized {unrealized_pct:+.1f}% | "
                f"market value ${p.market_value:,.0f} ({weight_map.get(p.symbol, 0):.1f}% of portfolio)"
            )
        positions_text = "\n".join(positions_lines)

        # Build news text
        news_lines = []
        for i, article in enumerate(top_articles, 1):
            age = f"{article.age_hours:.1f}h ago" if article.age_hours is not None else "unknown"
            news_lines.append(
                f"[{i}] {article.symbol} | {article.source} | {age}\n"
                f"    Title: {article.title}\n"
                f"    Summary: {article.summary[:300] if article.summary else 'N/A'}"
            )
        news_text = "\n\n".join(news_lines)

        # Select prompt components based on report type
        if report_type == "mid_market":
            focus = MID_MARKET_FOCUS
            summary_instr = MID_MARKET_SUMMARY
            narrative_instr = MID_MARKET_NARRATIVE
        elif report_type == "post_market":
            focus = POST_MARKET_FOCUS
            summary_instr = POST_MARKET_SUMMARY
            narrative_instr = POST_MARKET_NARRATIVE
        else:  # pre_market (default)
            focus = PRE_MARKET_FOCUS
            summary_instr = PRE_MARKET_SUMMARY
            narrative_instr = PRE_MARKET_NARRATIVE

        # Build extra context block for post-market (earnings calendar)
        extra_context = ""
        if report_type == "post_market" and earnings_data:
            lines = ["=== UPCOMING EARNINGS CALENDAR ==="]
            for sym, cal in earnings_data.items():
                lines.append(
                    f"  {sym}: earnings on {cal.get('earnings_date', 'TBD')} | "
                    f"EPS estimate: {cal.get('eps_estimate', 'N/A')} | "
                    f"Revenue estimate: {cal.get('revenue_estimate', 'N/A')}"
                )
            extra_context = "\n".join(lines) + "\n\n"

        prompt = _PROMPT_BASE.format(
            positions_text=positions_text,
            news_text=news_text,
            extra_context=extra_context,
            focus_instructions=focus,
            summary_instruction=summary_instr,
            narrative_instruction=narrative_instr,
        )

        try:
            raw = self._call_llm(prompt)
            return self._parse_portfolio_response(raw, top_articles, weight_map, positions)
        except Exception as e:
            logger.error(f"LLM analysis failed: {e}. Using fallback.")
            return self._fallback_analysis(top_articles, weight_map, positions)

    # -------------------------------------------------------------------------
    # LLM call dispatch
    # -------------------------------------------------------------------------
    def _call_llm(self, prompt: str) -> str:
        backend = getattr(self, "_backend", "fallback")
        if backend == "claude":
            return self._call_claude(prompt)
        elif backend == "gemini":
            return self._call_gemini(prompt)
        raise RuntimeError("No LLM backend available")

    def _call_claude(self, prompt: str) -> str:
        import anthropic

        # Reset daily counter if new day
        today = date.today().isoformat()
        if today != self._last_reset_date:
            self._daily_spent = 0.0
            self._last_reset_date = today

        if self._daily_spent >= self.settings.daily_spend_limit_usd:
            raise RuntimeError(
                f"Daily spend cap reached (${self._daily_spent:.4f} / "
                f"${self.settings.daily_spend_limit_usd:.2f})"
            )

        response = self._client.messages.create(
            model=self.settings.claude_model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )

        # Track cost: Haiku ~$0.80/$4 per M tokens
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        cost = (input_tokens * 0.80 + output_tokens * 4.0) / 1_000_000
        self._daily_spent += cost
        logger.info(f"Claude call: ${cost:.5f} | today total: ${self._daily_spent:.4f}")

        return response.content[0].text.strip()

    def _call_gemini(self, prompt: str) -> str:
        last_exc = None
        for attempt in range(3):
            try:
                response = self._gemini_client.models.generate_content(
                    model=self.settings.gemini_model,
                    contents=prompt,
                    config=self._gemini_config,
                )
                return response.text.strip()
            except Exception as e:
                last_exc = e
                if "503" in str(e) or "UNAVAILABLE" in str(e):
                    wait = 5 * (attempt + 1)
                    logger.warning(f"Gemini 503, retrying in {wait}s... (attempt {attempt+1}/3)")
                    time.sleep(wait)
                else:
                    raise
        raise last_exc

    # -------------------------------------------------------------------------
    # Response parsing
    # -------------------------------------------------------------------------
    @staticmethod
    def _repair_json(text: str) -> str:
        """
        Attempt to repair truncated JSON from a token-limit cutoff.

        Strategy: find the last complete top-level list item in stock_calls
        or articles, close all open brackets/braces, and return a valid JSON
        string so we get a partial-but-parseable result rather than an error.
        """
        # Strip markdown fences
        clean = text.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1].strip()
        if clean.endswith("```"):
            clean = clean[:-3].strip()

        # Try as-is first
        try:
            json.loads(clean)
            return clean
        except json.JSONDecodeError:
            pass

        # Truncate to the last complete object: walk backwards to find the
        # last '}' that closes a stock_call or article entry, then add the
        # minimum closing brackets needed to make valid JSON.
        # Step 1: find last '}' that is at indent level 2 (inside the array)
        last_good = clean.rfind("}",)
        if last_good == -1:
            return clean  # give up

        truncated = clean[:last_good + 1]

        # Step 2: count unclosed brackets/braces
        depth = 0
        in_str = False
        escape_next = False
        for ch in truncated:
            if escape_next:
                escape_next = False
                continue
            if ch == "\\" and in_str:
                escape_next = True
                continue
            if ch == '"' and not escape_next:
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1

        # Step 3: close open arrays/objects by appending the right closers
        # We assume the top structure is { ... "stock_calls": [ { ... } ] }
        # depth > 0 means we have that many unclosed openers remaining
        closing = ""
        # Heuristic: we know the structure. If depth == 3, we have
        # the outer object, one array, and one inner object open.
        # Just close with the right sequence.
        close_map = {1: "}", 2: "]}", 3: "]}"}
        if depth in close_map:
            closing = close_map[depth]
        elif depth > 0:
            closing = "]" * max(0, depth - 1) + "}" * min(1, depth)

        repaired = truncated + closing
        try:
            json.loads(repaired)
            logger.warning(
                f"Repaired truncated JSON (added {closing!r} to close {depth} open bracket(s))"
            )
            return repaired
        except json.JSONDecodeError:
            return clean  # repair failed, let caller handle

    def _parse_portfolio_response(
        self,
        text: str,
        top_articles: list[NewsArticle],
        weight_map: dict[str, float],
        positions: list,
    ) -> PortfolioAnalysis:
        """Parse LLM JSON response into PortfolioAnalysis."""
        try:
            clean = self._repair_json(text)
            data = json.loads(clean)
            overall = data.get("overall_summary", "")
            macro_note = data.get("macro_note", "")
            raw_articles = data.get("articles", [])
            raw_calls = data.get("stock_calls", [])

            # Map parsed articles back to original NewsArticle data
            article_map = {i + 1: a for i, a in enumerate(top_articles)}
            article_results: list[ArticleAnalysis] = []

            for item in raw_articles:
                rank = item.get("rank", 0)
                original = article_map.get(rank)
                if not original:
                    continue
                pub_str = ""
                if original.published:
                    pub_str = original.published.strftime("%m/%d %H:%M")

                article_results.append(ArticleAnalysis(
                    rank=rank,
                    symbol=item.get("symbol", original.symbol),
                    title=original.title,
                    url=original.url,
                    source=original.source,
                    published_str=pub_str,
                    sentiment=item.get("sentiment", "neutral"),
                    impact_summary=item.get("impact_summary", ""),
                    portfolio_pct=weight_map.get(original.symbol, 0),
                ))

            # Build per-stock calls
            # Get cost-per-share from positions (use cost_per_share if available)
            cost_map: dict[str, float] = {}
            price_map: dict[str, float] = {}
            for p in positions:
                if p.symbol not in cost_map:
                    cps = getattr(p, "cost_per_share", None)
                    if not cps and p.quantity:
                        cps = p.cost_basis / p.quantity
                    cost_map[p.symbol] = cps or 0
                    price_map[p.symbol] = p.current_price

            stock_calls: list[StockCall] = []
            for item in raw_calls:
                sym = item.get("symbol", "")
                current = price_map.get(sym, 0)
                cost = cost_map.get(sym, 0)
                unrealized_pct = ((current - cost) / cost * 100) if cost else 0

                # Parse structured prediction fields
                pred_dir = item.get("predicted_direction", "flat").lower()
                if pred_dir not in ("up", "down", "flat"):
                    pred_dir = "flat"
                try:
                    pred_mag = float(item.get("predicted_magnitude_pct", 0) or 0)
                except (TypeError, ValueError):
                    pred_mag = 0.0

                stock_calls.append(StockCall(
                    symbol=sym,
                    net_sentiment=item.get("net_sentiment", "neutral"),
                    estimated_move=item.get("estimated_move", "N/A"),
                    trend_narrative=item.get("trend_narrative", ""),
                    recommendation=item.get("recommendation", "HOLD"),
                    action_detail=item.get("action_detail", ""),
                    stop_loss=item.get("stop_loss", "N/A"),
                    price_target_short=item.get("price_target_short", "N/A"),
                    predicted_direction=pred_dir,
                    predicted_magnitude_pct=pred_mag,
                    current_price=current,
                    cost_basis=cost,
                    unrealized_pct=unrealized_pct,
                ))

            # Sort by portfolio weight descending
            stock_calls.sort(key=lambda c: weight_map.get(c.symbol, 0), reverse=True)

            logger.info(
                f"Analysis complete: {len(article_results)} articles, "
                f"{len(stock_calls)} stock calls"
            )
            return PortfolioAnalysis(
                articles=article_results,
                stock_calls=stock_calls,
                overall_summary=overall,
                macro_note=macro_note,
            )

        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to parse LLM response: {e}")
            logger.debug(f"Raw response: {text!r}")
            return self._fallback_analysis(top_articles, weight_map, positions)

    def _fallback_analysis(
        self,
        articles: list[NewsArticle],
        weight_map: dict[str, float],
        positions: list,
    ) -> PortfolioAnalysis:
        """Keyword-based fallback when no LLM is available."""
        BULLISH = ["surge", "rally", "beat", "upgrade", "growth", "profit",
                   "record", "strong", "exceed", "raise", "buy", "gain"]
        BEARISH = ["decline", "fall", "miss", "downgrade", "loss", "warning",
                   "cut", "drop", "weak", "layoff", "sell", "concern",
                   "tariff", "trade war", "recession", "inflation"]

        article_results = []
        for i, article in enumerate(articles, 1):
            text = (article.title + " " + article.summary).lower()
            pos = sum(1 for kw in BULLISH if kw in text)
            neg = sum(1 for kw in BEARISH if kw in text)
            if pos > neg + 1:
                sentiment = "bullish"
                summary = f"News appears positive for {article.symbol} (keyword analysis)."
            elif neg > pos + 1:
                sentiment = "bearish"
                summary = f"News appears negative for {article.symbol} (keyword analysis)."
            else:
                sentiment = "neutral"
                summary = f"Mixed signals for {article.symbol} (keyword analysis)."

            pub_str = article.published.strftime("%m/%d %H:%M") if article.published else ""
            article_results.append(ArticleAnalysis(
                rank=i,
                symbol=article.symbol,
                title=article.title,
                url=article.url,
                source=article.source,
                published_str=pub_str,
                sentiment=sentiment,
                impact_summary=summary,
                portfolio_pct=weight_map.get(article.symbol, 0),
            ))

        # Build fallback stock calls
        seen_syms: set[str] = set()
        sentiment_by_sym: dict[str, list[str]] = {}
        for a in articles:
            if a.symbol and a.symbol != "MACRO":
                sentiment_by_sym.setdefault(a.symbol, []).append(
                    article_results[[r.rank for r in article_results].index(
                        articles.index(a) + 1
                    )].sentiment if False else "neutral"
                )

        stock_calls: list[StockCall] = []
        sorted_positions = sorted(positions, key=lambda p: p.market_value, reverse=True)
        for p in sorted_positions:
            if p.symbol in seen_syms:
                continue
            seen_syms.add(p.symbol)
            cost = getattr(p, "cost_per_share", 0)
            unrealized_pct = ((p.current_price - cost) / cost * 100) if cost else 0
            stock_calls.append(StockCall(
                symbol=p.symbol,
                net_sentiment="neutral",
                estimated_move="N/A",
                trend_narrative="LLM unavailable — keyword analysis only.",
                recommendation="HOLD",
                action_detail="LLM API unavailable. Monitor positions and news manually.",
                stop_loss="N/A",
                price_target_short="N/A",
                current_price=p.current_price,
                cost_basis=cost,
                unrealized_pct=unrealized_pct,
            ))

        return PortfolioAnalysis(
            articles=article_results,
            stock_calls=stock_calls,
            overall_summary="Keyword-based analysis — LLM API unavailable.",
            macro_note="",
        )
