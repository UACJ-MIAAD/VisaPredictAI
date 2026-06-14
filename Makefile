# One-command operations for the VisaBulletinScraping pipeline.
# Override the interpreter with: make test PY=python
PY ?= ante/bin/python

.PHONY: help install scrape panel figures audit test lint typecheck check all

help:
	@echo "install  - install pinned dependencies (+ ruff)"
	@echo "scrape   - run both scrapers (network, ~4 min)"
	@echo "panel    - build the consolidated long panel"
	@echo "figures  - regenerate the PNG figures"
	@echo "audit    - data-quality + mega audits"
	@echo "test     - run the full test suite (offline)"
	@echo "lint     - ruff check"
	@echo "typecheck- mypy"
	@echo "check    - lint + typecheck + test"
	@echo "all      - scrape -> panel -> test -> figures -> audit"

install:
	$(PY) -m pip install -r requirements.txt
	$(PY) -m pip install ruff==0.15.17 mypy==2.1.0

scrape:
	$(PY) scrape_visa_bulletins.py
	$(PY) scrape_family_visa_bulletins.py

panel:
	$(PY) build_panel.py

figures:
	$(PY) visualize_visa_wait_times.py
	$(PY) visualize_family_wait_times.py

audit:
	$(PY) audit_data_quality.py
	$(PY) mega_audit.py

test:
	$(PY) tests/test_parsers.py
	$(PY) tests/test_extraction.py
	$(PY) tests/test_panel_integrity.py

lint:
	$(PY) -m ruff check .

typecheck:
	$(PY) -m mypy --ignore-missing-imports *.py tests/*.py

check: lint typecheck test

all: scrape panel test figures audit
