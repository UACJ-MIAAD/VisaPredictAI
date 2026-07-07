# One-command operations for the VisaPredictAI pipeline.
# Override the interpreter with: make test PY=python
PY ?= ante/bin/python
DVC ?= ante/bin/dvc

.PHONY: help install model-install freeze scrape panel db news repro repro-force dag challenger shadow model-card drift figures audit test test-model lint typecheck check all update eda eda-facts eda-all eda-report fe-facts fe-figures fe-report fe-all compare report validate key-facts consistency web-forecasts score-forecasts derive-band80 significance horizon-facts auto-arima paper-figures sync mlflow-sync

help:
	@echo "install  - editable install with pinned runtime + dev tools (pip install -e .[dev])"
	@echo "model-install - install the modeling extra too (darts/torch/xgboost/prophet)"
	@echo "eda      - regenerate the EDA figures (needs model-install + db)"
	@echo "eda-facts  - panel-wide EDA census -> reports/eda/eda_facts.json"
	@echo "eda-all    - full EDA: census + per-series + distributional + gallery figures"
	@echo "eda-report - standalone EDA PDF report -> reports/eda/eda_report.pdf"
	@echo "fe-facts   - FE + cleaning catalog -> reports/fe/fe_facts.json"
	@echo "fe-figures - bilingual FE gallery (7 figs x 4 variants) + .tex PDFs"
	@echo "fe-report  - standalone bilingual FE PDF report -> reports/fe/fe_report.pdf (+ en/)"
	@echo "fe-all     - full FE: catalog + gallery + report"
	@echo "compare  - walk-forward comparison of the 8 models -> reports/eval/model_comparison.csv"
	@echo "report   - results table + holdout figure from the comparison"
	@echo "web-forecasts   - per-series 12-month forecasts for the web demo + archive vintage (needs db)"
	@echo "score-forecasts - prospective scoring: frozen forecasts vs realized cutoffs (needs db + ledger)"
	@echo "derive-band80   - re-derive BAND80_RATIO on a disjoint split (read-only; prints held-out cov80)"
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
	$(PY) -m pipeline.freeze_snapshots

scrape:
	$(PY) -m pipeline.scrape_all

panel:
	$(PY) -m pipeline.build_panel

db:
	$(PY) -m pipeline.build_database

news:
	$(PY) -m pipeline.build_bulletins_json

repro:  ## reconstruye TODO el DAG de datos determinísticamente (solo lo que cambió) con DVC
	$(DVC) repro

repro-force:  ## fuerza re-ejecutar todas las etapas del DAG (ignora la cache)
	$(DVC) repro --force

dag:  ## imprime el grafo de dependencias del pipeline
	$(DVC) dag

challenger:  ## evalúa campeón vs retadores (Wilcoxon+Holm) -> reports/governance/champion_challenger.{json,md}
	$(PY) experiments/run_champion_challenger.py --mlflow

shadow:  ## congela la añada del mejor retador en el shadow ledger (AO6) -> reports/prospective/forecast_log_shadow.csv
	$(PY) experiments/freeze_shadow.py

model-card:  ## regenera reports/governance/MODEL_CARD.md (tarjeta de modelo + linaje) desde key_facts
	$(PY) experiments/build_model_card.py

drift:  ## monitor de drift ML (desempeño+cobertura del ledger + datos del último boletín)
	$(PY) experiments/check_drift.py

# Local refresh after the CI Action commits a new bulletin: pull the new
# CSVs/panel/news, sync the new frozen HTML from S3, rebuild the DuckDB and
# figures (both gitignored/regenerable). Mirrors EpiForecast's `update-week`.
update:
	git pull origin main
	aws s3 sync s3://visapredictai-raw-snapshots/raw-html/ data/snapshots/ --quiet
	$(PY) -m pipeline.build_database
	$(PY) experiments/visualize_wait_times.py
	@$(PY) -c "import pandas as pd; print('>>> Panel al día. Último boletín:', pd.read_csv('data/processed/visa_panel_long.csv').bulletin_date.max())"

figures:
	$(PY) experiments/visualize_wait_times.py

audit:
	$(PY) -m pipeline.mega_audit

test:
	$(PY) -m pytest

# Capa de modelado (requiere `make model-install`): mide cobertura de vp_model con
# piso propio (el gate por defecto cubre la capa de datos; este, el modelado).
test-model:
	$(PY) -m pytest -o addopts="" --cov=vp_model --cov-report=term-missing --cov-fail-under=55 \
		tests/test_dataset.py tests/test_eda_preprocess.py tests/test_models.py \
		tests/test_walkforward.py tests/test_intervals_significance.py tests/test_config_report.py \
		tests/test_series_characterization.py tests/test_missingness.py tests/test_feature_select.py \
		tests/test_feature_builder.py \
		tests/test_ensemble.py tests/test_model_regression.py tests/test_champion.py

# Reproducir los resultados (requiere `make model-install` + `make db`):
eda:
	$(PY) -m vp_model.plots

eda-facts:  ## censo estadístico EDA del panel completo -> reports/eda/eda_facts.json
	$(PY) experiments/build_eda_facts.py

eda-all: eda-facts eda  ## EDA COMPLETO: censo + figuras per-series + distribucionales + galería
	$(PY) experiments/make_eda_figures.py
	$(PY) experiments/make_gallery_figures.py

eda-report:  ## reporte PDF standalone del EDA (galería + hallazgos) -> reports/eda/eda_report.pdf
	$(PY) experiments/build_eda_report.py

fe-facts:  ## catálogo FE + limpieza (decisiones, ledger, selección FRESH) -> reports/fe/fe_facts.json
	$(PY) experiments/build_fe_facts.py

fe-figures:  ## galería FE bilingüe f01-f07 (es/en × clara/oscura) + PDFs vector del .tex
	$(PY) experiments/make_fe_figures.py

fe-report:  ## reporte PDF bilingüe de FE/limpieza -> reports/fe/fe_report.pdf (+ en/) y su test
	$(PY) experiments/build_fe_report.py
	$(PY) tests/test_fe_report.py

fe-all: fe-facts fe-figures fe-report  ## FE COMPLETO: catálogo + galería + reporte

compare:
	$(PY) -m vp_model.run_comparison

report:
	$(PY) -m vp_model.report

web-forecasts:  ## pronósticos futuros por serie para el demostrador web (tracked en MLflow)
	$(PY) experiments/generate_web_forecasts.py

score-forecasts:  ## evaluación PROSPECTIVA: pronósticos congelados vs cortes reales (scorecard + MLflow)
	$(PY) experiments/score_forecasts.py

derive-band80:  ## re-deriva BAND80_RATIO en split disjunto (read-only; imprime cov80 held-out)
	$(PY) experiments/derive_band80_ratio.py

significance:  ## Friedman-Nemenyi + MCS + DM para el paper (read-only; figura CD)
	$(PY) experiments/significance_tables.py

horizon-facts:  ## campeón POR HORIZONTE + significancia (rolling, F-only, hasta 5 años) -> reports/eval/horizon_facts.json + reports/latex/horizon_champion.tex
	$(PY) experiments/build_horizon_facts.py

auto-arima:  ## baseline Auto-ARIMA (AICc) bajo el walk-forward del pool -> reports/eval/auto_arima_baseline.csv
	$(PY) experiments/auto_arima_baseline.py

paper-figures:  ## regenera las figuras del paper MICAI desde el pipeline -> reports/paper_micai/Figures/
	$(PY) reports/paper_micai/make_paper_figures.py

key-facts:  ## regenera la fuente única de verdad reports/governance/key_facts.json (+ macros .tex) del pipeline
	$(PY) experiments/build_key_facts.py

consistency:  ## GUARDIÁN: web/LaTeX/paper/README/docs deben dar el MISMO número (vs key_facts.json)
	$(PY) tools/check_consistency.py

lint:
	$(PY) -m ruff check .

typecheck:
	$(PY) -m mypy --ignore-missing-imports --explicit-package-bases vp_data/*.py pipeline/*.py vp_model/*.py tests/*.py tools/*.py experiments/*.py

validate:
	bash tools/validate_structure.sh

check: validate consistency lint typecheck test

all: freeze scrape panel db test figures audit

sync:  ## todo machin: MLflow + DVC->S3 + git (tras una corrida)
	bash experiments/sync_all.sh

# AO9 (decision): MLflow is a manually-synced HISTORICAL ARCHIVE, not a live dashboard.
# The durable/canonical record is the CSV/JSON committed in git; sync when you want the UI.
mlflow-sync:  ## staging JSONL -> mlflow.db (archivo histórico; corre en ante_nf)
	ante_nf/bin/python experiments/sync_mlflow.py
