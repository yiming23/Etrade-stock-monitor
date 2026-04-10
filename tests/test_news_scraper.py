"""Tests for news scraper module."""

from datetime import datetime, timezone

import pytest

from src.news.scraper import NewsArticle, NewsScraper
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
def scraper(settings):
    return NewsScraper(settings)


class TestNewsArticle:
    def test_dedup_key_strips_prefix(self):
        a1 = NewsArticle(
            title="Breaking: Apple announces new product",
            url="https://example.com/1",
            source="Test",
            published=None,
            summary="",
            symbol="AAPL",
        )
        a2 = NewsArticle(
            title="Apple announces new product",
            url="https://example.com/2",
            source="Test",
            published=None,
            summary="",
            symbol="AAPL",
        )
        assert a1.dedup_key() == a2.dedup_key()

    def test_age_hours(self):
        article = NewsArticle(
            title="Test",
            url="",
            source="Test",
            published=datetime.now(timezone.utc),
            summary="",
            symbol="TEST",
        )
        assert article.age_hours is not None
        assert article.age_hours < 0.1  # Just created

    def test_age_hours_none_when_no_date(self):
        article = NewsArticle(
            title="Test",
            url="",
            source="Test",
            published=None,
            summary="",
            symbol="TEST",
        )
        assert article.age_hours is None


class TestNewsScraper:
    def test_score_articles_authority(self, scraper):
        articles = [
            NewsArticle(
                title="Reuters Article",
                url="https://reuters.com/article",
                source="Reuters",
                published=datetime.now(timezone.utc),
                summary="Summary",
                symbol="TEST",
            ),
            NewsArticle(
                title="Unknown Source Article",
                url="https://random-blog.com/article",
                source="Random Blog",
                published=datetime.now(timezone.utc),
                summary="Summary",
                symbol="TEST",
            ),
        ]
        scraper._score_articles(articles)
        # Reuters should score higher than unknown source
        assert articles[0].relevance_score > articles[1].relevance_score

    def test_score_articles_recency(self, scraper):
        from datetime import timedelta

        articles = [
            NewsArticle(
                title="New Article",
                url="https://example.com/1",
                source="Test",
                published=datetime.now(timezone.utc),
                summary="",
                symbol="TEST",
            ),
            NewsArticle(
                title="Old Article",
                url="https://example.com/2",
                source="Test",
                published=datetime.now(timezone.utc) - timedelta(days=3),
                summary="",
                symbol="TEST",
            ),
        ]
        scraper._score_articles(articles)
        assert articles[0].relevance_score > articles[1].relevance_score
