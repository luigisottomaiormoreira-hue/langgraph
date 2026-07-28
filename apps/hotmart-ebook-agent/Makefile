.PHONY: install format lint test demo

TEST ?= tests

install:
	uv sync --all-extras

format:
	uv run ruff format hotmart_agent tests
	uv run ruff check --fix hotmart_agent tests

lint:
	uv run ruff check hotmart_agent tests
	uv run ruff format --check hotmart_agent tests

test:
	uv run pytest $(TEST)

demo:
	uv run python -m hotmart_agent "me dá um resumo geral do produto"
