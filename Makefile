.PHONY: install test lint format format-check build publish clean help

help:
	@echo "Makefile commands:"
	@echo "  install        - Install dependencies"
	@echo "  test           - Run tests"
	@echo "  lint           - Lint the code"
	@echo "  format         - Format the code"
	@echo "  format-check   - Check code formatting"
	@echo "  build          - Build the project"
	@echo "  publish        - Publish the project"
	@echo "  publish-dry    - Dry run of publish"
	@echo "  clean          - Clean build artifacts and caches"

install:
	uv sync

test:
	uv run pytest

lint:
	uv run ruff check .

format:
	uv run ruff format .
	uv run ruff check --fix .

format-check:
	uv run ruff format --check .
	uv run ruff check .

build:
	uv build

publish: build
	uv publish

publish-dry:
	uv publish --dry-run

clean:
	rm -rf dist/ .ruff_cache/ __pycache__ .pytest_cache
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
