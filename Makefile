.PHONY: install install-dev check test run docker-up docker-down backup preflight validate release

install:
	python -m pip install -r requirements.lock

install-dev:
	python -m pip install -r requirements-dev.txt

check:
	python -m compileall -q .
	ruff check .

format:
	ruff format .
	ruff check --fix .

test:
	pytest

run:
	python coach_bot.py

preflight:
	python scripts/preflight.py

# Full quality gate: compile, lint, tests, evaluations, preflight, clean package.
# Ruff/compile are scoped to project source (the .venv is excluded in pyproject).
validate:
	python -m compileall -q $(filter-out .venv,$(wildcard *.py)) scripts tests
	ruff check . --exclude .venv
	pytest -q -o addopts="" --maxfail=0
	python scripts/run_evaluations.py
	python scripts/preflight.py --skip-runtime-secrets
	python scripts/build_release.py

release:
	python scripts/build_release.py

backup:
	python scripts/backup.py

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down
