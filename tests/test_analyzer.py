"""Tests for the stock analyzer module."""

from datetime import datetime, timezone

import pytest

from src.analysis.analyzer import StockAnalyzer, StockAnalysis
from src.news.scraper import NewsArticle
from src.utils.config import Settings


@pytest.fixture
def settings():
    return Settings(
        etrade_consumer_key="test",
        etrade_consumer_secret="test",
        anthropic_api_key="test",
        gmail_address="test@gmail.com",
        gmail_app_password="test",
        recipient_email="test@gmail.com",
    )


@pytest.fixture
def analyzer(settings):
    return StockAnalyzer(settings)


class TestFallbackAnalysis:
    def test_no_articles(self, analyzer):
        result = analyzer._fallback_analysis("TEST", [])
        assert result.symbol == "TEST"
        assert result.sentiment == "neutral"
        assert result.confidence == "low"

    def test_positive_keywords(self, analyzer):
        articles = [
            NewsArticle(
                title="Stock surges on record profit and growth beat",
                url="",
                source="Test",
                published=datetime.now(timezone.utc),
                summary="The company reported record earnings and surging revenue growth.",
                symbol="TEST",
            ),
        ]
        result = analyzer._fallback_analysis("TEST", articles)
        assert result.sentiment == "bullish"

    def test_negative_keywords(self, analyzer):
        articles = [
            NewsArticle(
                title="Stock falls on earnings miss and downgrade warning",
                url="",
                source="Test",
                published=datetime.now(timezone.utc),
                summary="Analysts downgrade the stock following significant decline and loss.",
                symbol="TEST",
            ),
        ]
        result = analyzer._fallback_analysis("TEST", articles)
        assert result.sentiment == "bearish"


class TestParseResponse:
    def test_valid_json(self, analyzer):
        text = '''{
            "impact_summary": "Stock likely to rise",
            "sentiment": "bullish",
            "confidence": "high",
            "key_factors": ["Strong earnings", "Market momentum"]
        }'''
        result = analyzer._parse_response("TEST", text)
        assert result.sentiment == "bullish"
        assert result.confidence == "high"
        assert len(result.key_factors) == 2

    def test_json_in_code_block(self, analyzer):
        text = '''```json
{
    "impact_summary": "Neutral outlook",
    "sentiment": "neutral",
    "confidence": "medium",
    "key_factors": ["Mixed signals"]
}
```'''
        result = analyzer._parse_response("TEST", text)
        assert result.sentiment == "neutral"

    def test_invalid_json(self, analyzer):
        text = "This is not valid JSON at all"
        result = analyzer._parse_response("TEST", text)
        assert result.confidence == "low"
