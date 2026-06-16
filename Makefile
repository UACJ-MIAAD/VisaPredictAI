# One-command operations for the VisaPredictAI pipeline.
# Override the interpreter with: make test PY=python
PY ?= ante/bin/python

.PHONY: help install freeze scrape panel db figures audit test lint typecheck check all

help:
	@echo "install  - editable install with pinned runtime + dev tools (pip install -e .[dev])"
	@echo "freeze   - fetch only newly published bulletins to data/snapshots/ (network; skip-if-exists)"
	@echo "scrape   - parse the frozen snapshots offline into the 3 sections (no network)"
	@echo "panel    - build the consolidated long panel"
	@echo "db       - load the star-schema DuckDB + Parquet export from the panel"
	@echo "figures  - regenerate the PNG figures"
	@echo "audit    - mega audit (data quality)"
	@echo "test     - run the full test suite (offline)"
	@echo "lint     - ruff check"
	@echo "typecheck- mypy"
	@echo "check    - lint + typecheck + test"
	@echo "all      - scrape -> panel -> db -> test -> figures -> audit"

install:
	$(PY) -m pip install -e ".[dev]"

freeze:
	$(PY) freeze_snapshots.py

scrape:
	$(PY) scrape_all.py

panel:
	$(PY) build_panel.py

db:
	$(PY) build_database.py

figures:
	$(PY) visualize_wait_times.py

audit:
	$(PY) mega_audit.py

test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check .

typecheck:
	$(PY) -m mypy --ignore-missing-imports *.py tests/*.py

check: lint typecheck test

all: freeze scrape panel db test figures audit
