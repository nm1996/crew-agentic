.PHONY: install dev test lint format check build clean

install:        ## Install the crew command for your user
	uv tool install --reinstall .

dev:            ## Create the development environment
	uv sync --dev

test:           ## Run the offline test suite
	uv run pytest

lint:           ## Lint and check formatting
	uv run ruff check .
	uv run ruff format --check .

format:         ## Auto-format and fix lint issues
	uv run ruff format .
	uv run ruff check --fix .

check: lint test

build:          ## Build sdist and wheel into dist/
	uv build

clean:
	rm -rf dist build .pytest_cache .ruff_cache
