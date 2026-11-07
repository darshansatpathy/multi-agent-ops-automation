.PHONY: install check test eval demo serve docker audit seed postgres-test

install:
	python -m venv .venv && .venv/bin/pip install -e ".[dev]"

check:
	ruff check . && ruff format --check . && mypy && pytest --cov --cov-fail-under=90 && opsagents eval --check

test:
	pytest

eval:
	opsagents eval --check --report eval-report.md

audit:
	pip-audit --skip-editable

demo:
	opsagents demo

serve:
	opsagents serve --port 8080

docker:
	docker build -t multi-agent-ops-automation .

seed:
	python scripts/generate_seed.py

postgres-test:
	docker compose up -d postgres
	OPS_TEST_DATABASE_URL=postgresql://ops:ops-local-only@localhost:5432/opsagents pytest -m postgres
