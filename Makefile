# One-command operations for the VisaPredictAI pipeline.
# Override the interpreter with: make test PY=python
PY ?= ante/bin/python

.PHONY: help install freeze scrape panel db news figures audit test lint typecheck check all update

help:
	@echo "install  - editable install with pinned runtime + dev tools (pip install -e .[dev])"
	@echo "update   - refresh local AFTER the CI committed a new bulletin (pull + snapshots + db + figures)"
	@echo "freeze   - fetch only newly published bulletins to data/snapshots/ (network; skip-if-exists)"
	@echo "scrape   - parse the frozen snapshots offline into the 3 sections (no network)"
	@echo "panel    - build the consolidated long panel"
	@echo "db       - load the star-schema DuckDB + Parquet export from the panel"
	@echo "news     - regenerate data/processed/bulletins.json (latest-bulletin feed for the website)"
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

news:
	$(PY) build_bulletins_json.py

# Local refresh after the CI Action commits a new bulletin: pull the new
# CSVs/panel/news, sync the new frozen HTML from S3, rebuild the DuckDB and
# figures (both gitignored/regenerable). Mirrors EpiForecast's `update-week`.
update:
	git pull origin main
	aws s3 sync s3://visapredictai-raw-snapshots/raw-html/ data/snapshots/ --quiet
	$(PY) build_database.py
	$(PY) visualize_wait_times.py
	@$(PY) -c "import pandas as pd; print('>>> Panel al día. Último boletín:', pd.read_csv('data/processed/visa_panel_long.csv').bulletin_date.max())"

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
