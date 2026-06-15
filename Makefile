# One-command operations for the VisaPredictAI pipeline.
# Override the interpreter with: make test PY=python
PY ?= ante/bin/python

.PHONY: help install scrape panel db figures audit test lint typecheck check all

help:
	@echo "install  - editable install with pinned runtime + dev tools (pip install -e .[dev])"
	@echo "scrape   - run both scrapers (network, ~4 min)"
	@echo "panel    - build the consolidated long panel"
	@echo "db       - load the star-schema DuckDB + Parquet export from the panel"
	@echo "figures  - regenerate the PNG figures"
	@echo "audit    - data-quality + mega audits"
	@echo "test     - run the full test suite (offline)"
	@echo "lint     - ruff check"
	@echo "typecheck- mypy"
	@echo "check    - lint + typecheck + test"
	@echo "all      - scrape -> panel -> db -> test -> figures -> audit"

install:
	$(PY) -m pip install -e ".[dev]"

scrape:
	$(PY) scrape_visa_bulletins.py
	$(PY) scrape_family_visa_bulletins.py

panel:
	$(PY) build_panel.py

db:
	$(PY) build_database.py

figures:
	$(PY) visualize_visa_wait_times.py
	$(PY) visualize_family_wait_times.py

audit:
	$(PY) audit_data_quality.py
	$(PY) mega_audit.py

test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check .

typecheck:
	$(PY) -m mypy --ignore-missing-imports *.py tests/*.py

check: lint typecheck test

all: scrape panel db test figures audit
