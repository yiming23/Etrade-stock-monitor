.PHONY: setup test lint format run run-once clean

# Setup
setup:
	bash scripts/setup.sh

# Run
run:
	python -m src.main

run-once:
	python -m src.main --once --type pre_market

run-post:
	python -m src.main --once --type post_market

# Testing
test:
	pytest tests/ -v --tb=short

test-cov:
	pytest tests/ -v --cov=src --cov-report=html

# Code quality
lint:
	ruff check src/ tests/
	mypy src/ --ignore-missing-imports

format:
	black src/ tests/
	ruff check --fix src/ tests/

# Cleanup
clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache htmlcov .mypy_cache .coverage
