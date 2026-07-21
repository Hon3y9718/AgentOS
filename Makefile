.PHONY: dev test lint migrate

dev:
	docker compose up

test:
	uv run --directory backend pytest

lint:
	uv run --directory backend ruff check --fix
	uv run --directory backend ruff format
	uv run --directory backend mypy app

migrate:
	uv run --directory backend alembic revision --autogenerate -m "$(m)"
	uv run --directory backend alembic upgrade head
