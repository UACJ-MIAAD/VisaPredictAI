# One-command operations for the VisaPredictAI pipeline.
# Override the interpreter with: make test PY=python
PY ?= ante/bin/python

.PHONY: help install model-install freeze scrape panel db news figures audit test test-model lint typecheck check all update eda compare report validate

help:
	@echo "install  - editable install with pinned runtime + dev tools (pip install -e .[dev])"
	@echo "model-install - install the modeling extra too (darts/torch/xgboost/prophet)"
	@echo "eda      - regenerate the EDA figures (needs model-install + db)"
	@echo "compare  - walk-forward comparison of the 8 models -> reports/model_comparison.csv"
	@echo "report   - results table + holdout figure from the comparison"
	@echo "test-model - run modeling tests with vp_model coverage (needs model-install)"
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
	@echo "validate - assert cookiecutter structure + no loose files"
	@echo "all      - scrape -> panel -> db -> test -> figures -> audit"

install:
	$(PY) -m pip install -e ".[dev]"

model-install:
	$(PY) -m pip install -e ".[dev,model]"

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

# Capa de modelado (requiere `make model-install`): mide cobertura de vp_model con
# piso propio (el gate por defecto cubre la capa de datos; este, el modelado).
test-model:
	$(PY) -m pytest -o addopts="" --cov=vp_model --cov-report=term-missing --cov-fail-under=55 \
		tests/test_dataset.py tests/test_eda_preprocess.py tests/test_models.py \
		tests/test_walkforward.py tests/test_intervals_significance.py tests/test_config_report.py \
		tests/test_features.py tests/test_missingness.py tests/test_feature_select.py \
		tests/test_ensemble.py

# Reproducir los resultados (requiere `make model-install` + `make db`):
eda:
	$(PY) -m vp_model.plots

compare:
	$(PY) -m vp_model.run_comparison

report:
	$(PY) -m vp_model.report

web-forecasts:  ## pronósticos futuros por serie para el demostrador web (tracked en MLflow)
	$(PY) experiments/generate_web_forecasts.py

score-forecasts:  ## evaluación PROSPECTIVA: pronósticos congelados vs cortes reales (scorecard + MLflow)
	$(PY) experiments/score_forecasts.py

lint:
	$(PY) -m ruff check .

typecheck:
	$(PY) -m mypy --ignore-missing-imports *.py vp_model/*.py tests/*.py

validate:
	bash tools/validate_structure.sh

check: validate lint typecheck test

all: freeze scrape panel db test figures audit

sync:  ## todo machin: MLflow + DVC->S3 + git (tras una corrida)
	bash experiments/sync_all.sh
