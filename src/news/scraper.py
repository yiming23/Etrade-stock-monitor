"""
News scraper — Yahoo Finance + Google News RSS + Macro/Political news.

Three sources:
  1. Yahoo Finance (yfinance) — per-stock earnings/analyst news
  2. Google News RSS — per-stock recent headlines
  3. Macro RSS feeds — market-wide political, economic, geopolitical news
     (Reuters, AP, MarketWatch top stories, assigned symbol="MACRO")
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import time

import feedparser
import requests
import yfinance as yf
from bs4 import BeautifulSoup

from src.utils.config import Settings
from src.utils.logger import get_logger

logger = get_logger(__name__)

AUTHORITY_DOMAINS = {
    "reuters.com": 10,
    "bloomberg.com": 10,
    "apnews.com": 10,
    "wsj.com": 9,
    "cnbc.com": 9,
    "ft.com": 9,
    "marketwatch.com": 8,
    "barrons.com": 8,
    "seekingalpha.com": 7,
    "finance.yahoo.com": 7,
    "investors.com": 7,
    "benzinga.com": 6,
    "fool.com": 5,
}

# Macro RSS feeds — broad market/political/economic news
MACRO_RSS_FEEDS = [
    ("Reuters Business", "https://feeds.reuters.com/reuters/businessNews"),
    ("Reuters Markets",  "https://feeds.reuters.com/reuters/companyNews"),
    ("AP Business",      "https://feeds.apnews.com/rss/apf-business"),
    ("MarketWatch Top",  "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("Google News Market","https://news.google.com/rss/search?q=stock+market+tariff+war+fed+economy&hl=en-US&gl=US&ceid=US:en"),
]


@dataclass
class NewsArticle:
    """A single news article."""
    title: str
    url: str
    source: str
    published: datetime | None
    summary: str
    symbol: str           # stock ticker or "MACRO" for market-wide news
    relevance_score: float = 0.0

    @property
    def age_hours(self) -> float | None:
        if not self.published:
            return None
        return (datetime.now(timezone.utc) - self.published).total_seconds() / 3600

    def dedup_key(self) -> str:
        normalized = self.title.lower().strip()
        for prefix in ["breaking:", "update:", "exclusive:", "watch:"]:
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix):].strip()
        return hashlib.md5(normalized[:80].encode()).hexdigest()


class NewsScraper:
    """Multi-source news scraper."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.max_per_stock = settings.max_news_per_stock
        self.top_count = settings.top_news_count
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        })

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------
    def fetch_all_news(self, symbols: list[str]) -> dict[str, list[NewsArticle]]:
        """
        Fetch news for all symbols + macro news.
        Returns {symbol: [articles], "MACRO": [articles]}.
        """
        result: dict[str, list[NewsArticle]] = {}

        # Per-stock news
        for symbol in symbols:
            try:
                result[symbol] = self._fetch_symbol_news(symbol)
            except Exception as e:
                logger.error(f"Failed to fetch news for {symbol}: {e}")
                result[symbol] = []
            time.sleep(0.8)

        # Macro/political/market-wide news
        result["MACRO"] = self._fetch_macro_news()

        total = sum(len(v) for v in result.values())
        logger.info(f"Fetched {total} total articles "
                    f"({len(symbols)} stocks + macro)")
        return result

    def select_top_portfolio_news(
        self,
        news_by_symbol: dict[str, list[NewsArticle]],
        positions: list,
        top_n: int = 5,
    ) -> list[NewsArticle]:
        """
        Select top N most important articles across all holdings + macro.

        Scoring weights:
          40% — Recency
          35% — Portfolio weight  (MACRO gets median portfolio weight)
          25% — Impact keywords
        """
        if not positions:
            return []

        # Aggregate market value by symbol (handle duplicate positions)
        value_by_symbol: dict[str, float] = {}
        for pos in positions:
            value_by_symbol[pos.symbol] = value_by_symbol.get(pos.symbol, 0) + pos.market_value
        total_value = sum(value_by_symbol.values()) or 1.0

        weight_map: dict[str, float] = {
            sym: val / total_value for sym, val in value_by_symbol.items()
        }
        # MACRO news gets the median portfolio weight so it can compete fairly
        median_weight = sorted(weight_map.values())[len(weight_map) // 2] if weight_map else 0.15
        weight_map["MACRO"] = median_weight

        all_articles: list[NewsArticle] = []
        for articles in news_by_symbol.values():
            all_articles.extend(articles)

        if not all_articles:
            return []

        for article in all_articles:
            recency   = self._recency_score(article)
            weight    = weight_map.get(article.symbol, 0) * 10
            impact    = self._impact_keyword_score(article)
            authority = self._authority_score(article)
            article.relevance_score = (
                recency   * 0.40
                + weight  * 0.35
                + impact  * 0.25
                + authority * 0.05
            )

        all_articles.sort(key=lambda a: a.relevance_score, reverse=True)

        seen: set[str] = set()
        top: list[NewsArticle] = []
        for article in all_articles:
            key = article.dedup_key()
            if key not in seen:
                seen.add(key)
                top.append(article)
            if len(top) >= top_n:
                break

        macro_count = sum(1 for a in top if a.symbol == "MACRO")
        logger.info(
            f"Top {len(top)} articles selected "
            f"({len(top) - macro_count} stock-specific, {macro_count} macro)"
        )
        for i, a in enumerate(top, 1):
            age = f"{a.age_hours:.1f}h" if a.age_hours is not None else "?"
            wt  = weight_map.get(a.symbol, 0)
            logger.debug(
                f"  #{i} [{a.symbol} {wt:.0%}] "
                f"score={a.relevance_score:.2f} | {age} ago | {a.title[:55]}"
            )
        return top

    # -------------------------------------------------------------------------
    # Per-symbol news sources
    # -------------------------------------------------------------------------
    def _fetch_symbol_news(self, symbol: str) -> list[NewsArticle]:
        """Fetch + deduplicate + score news for one symbol."""
        articles = []
        articles.extend(self._fetch_yfinance_news(symbol))
        articles.extend(self._fetch_google_news_rss(symbol, query=f"{symbol}+stock"))

        seen: set[str] = set()
        unique = []
        for a in articles:
            k = a.dedup_key()
            if k not in seen:
                seen.add(k)
                unique.append(a)

        self._score_articles(unique)
        unique.sort(key=lambda a: a.relevance_score, reverse=True)
        logger.info(f"[{symbol}] {len(articles)} raw → {len(unique)} unique")
        return unique  # return ALL for Claude to summarize; top-N selection happens later

    def _fetch_yfinance_news(self, symbol: str) -> list[NewsArticle]:
        articles = []
        try:
            ticker = yf.Ticker(symbol)
            for item in (ticker.news or [])[:self.max_per_stock]:
                pub_time = None
                if item.get("providerPublishTime"):
                    pub_time = datetime.fromtimestamp(
                        item["providerPublishTime"], tz=timezone.utc)
                elif isinstance(item.get("content"), dict):
                    try:
                        pub_time = datetime.fromisoformat(
                            item["content"].get("pubDate", "").replace("Z", "+00:00"))
                    except (ValueError, TypeError):
                        pass

                title  = item.get("title", "") or (item.get("content") or {}).get("title", "")
                url    = item.get("link", "") or (item.get("content") or {}).get("canonicalUrl", {}).get("url", "")
                source = item.get("publisher", "") or (item.get("content") or {}).get("provider", {}).get("displayName", "Yahoo Finance")
                summary = (item.get("content") or {}).get("summary", "") if isinstance(item.get("content"), dict) else ""

                if title:
                    articles.append(NewsArticle(title=title, url=url, source=source,
                                                published=pub_time,
                                                summary=summary[:400] if summary else "",
                                                symbol=symbol))
        except Exception as e:
            logger.warning(f"[{symbol}] yfinance news failed: {e}")
        return articles

    def _fetch_google_news_rss(self, symbol: str, query: str) -> list[NewsArticle]:
        articles = []
        try:
            rss_url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
            feed = feedparser.parse(rss_url)
            for entry in feed.entries[:self.max_per_stock]:
                pub_time = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    pub_time = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)

                title = entry.get("title", "")
                source = "Google News"
                if " - " in title:
                    parts = title.rsplit(" - ", 1)
                    title, source = parts[0].strip(), parts[1].strip()

                summary = ""
                desc = entry.get("description", "")
                if desc:
                    soup = BeautifulSoup(desc, "lxml")
                    summary = soup.get_text(strip=True)[:400]

                articles.append(NewsArticle(
                    title=title, url=entry.get("link", ""),
                    source=source, published=pub_time,
                    summary=summary, symbol=symbol,
                ))
        except Exception as e:
            logger.warning(f"[{symbol}] Google News RSS failed: {e}")
        return articles

    # -------------------------------------------------------------------------
    # Macro/political/economic news
    # -------------------------------------------------------------------------
    def _fetch_macro_news(self) -> list[NewsArticle]:
        """
        Fetch market-wide news: tariffs, trade war, Fed, geopolitics, economy.
        Assigns symbol="MACRO" so they can compete in cross-portfolio ranking.
        """
        articles: list[NewsArticle] = []

        for feed_name, rss_url in MACRO_RSS_FEEDS:
            try:
                feed = feedparser.parse(rss_url)
                for entry in feed.entries[:15]:
                    pub_time = None
                    if hasattr(entry, "published_parsed") and entry.published_parsed:
                        pub_time = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)

                    title = entry.get("title", "").strip()
                    if not title:
                        continue
                    # Strip "Source - " suffix from Google News titles
                    source = feed_name
                    if " - " in title and "Google News" in feed_name:
                        parts = title.rsplit(" - ", 1)
                        title, source = parts[0].strip(), parts[1].strip()

                    summary = ""
                    desc = entry.get("summary", "") or entry.get("description", "")
                    if desc:
                        soup = BeautifulSoup(desc, "lxml")
                        summary = soup.get_text(strip=True)[:400]

                    articles.append(NewsArticle(
                        title=title, url=entry.get("link", ""),
                        source=source, published=pub_time,
                        summary=summary, symbol="MACRO",
                    ))
            except Exception as e:
                logger.warning(f"Macro feed '{feed_name}' failed: {e}")
            time.sleep(0.3)

        # Deduplicate
        seen: set[str] = set()
        unique = []
        for a in articles:
            k = a.dedup_key()
            if k not in seen:
                seen.add(k)
                unique.append(a)

        self._score_articles(unique)
        unique.sort(key=lambda a: a.relevance_score, reverse=True)
        logger.info(f"[MACRO] {len(articles)} raw → {len(unique)} unique macro articles")
        return unique

    # -------------------------------------------------------------------------
    # Scoring helpers
    # -------------------------------------------------------------------------
    def _score_articles(self, articles: list[NewsArticle]) -> None:
        for article in articles:
            article.relevance_score = (
                self._authority_score(article) * 0.5
                + self._recency_score(article) * 0.5
            )

    def _recency_score(self, article: NewsArticle) -> float:
        h = article.age_hours
        if h is None: return 4.0
        if h < 1:     return 10.0
        if h < 3:     return 9.0
        if h < 6:     return 8.0
        if h < 12:    return 6.0
        if h < 24:    return 4.0
        if h < 48:    return 2.0
        return 1.0

    def _authority_score(self, article: NewsArticle) -> float:
        src = article.source.lower()
        url = article.url.lower()
        for domain, score in AUTHORITY_DOMAINS.items():
            if domain in url or domain in src:
                return float(score)
        return 3.0

    def _impact_keyword_score(self, article: NewsArticle) -> float:
        HIGH = [
            "tariff", "trade war", "sanction", "war", "invasion", "attack",
            "earnings", "revenue", "guidance", "beat", "miss", "upgrade",
            "downgrade", "acquisition", "merger", "deal", "buyback", "layoff",
            "fda", "approved", "rejected", "lawsuit", "sec", "investigation",
            "interest rate", "fed", "inflation", "recession", "default",
            "crash", "surge", "plunge", "rally", "record",
        ]
        MEDIUM = [
            "analyst", "target price", "outlook", "growth", "profit", "loss",
            "margin", "partnership", "contract", "ceo", "executive", "expand",
            "sanctions", "geopolit", "election", "supply chain", "oil",
        ]
        text = (article.title + " " + article.summary).lower()
        high   = sum(1 for kw in HIGH   if kw in text)
        medium = sum(1 for kw in MEDIUM if kw in text)
        return min(10.0, (high * 2 + medium) * 1.5)
