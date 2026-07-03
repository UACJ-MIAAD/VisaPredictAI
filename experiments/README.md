# experiments/

Scripts de modelado y experimentación del Objetivo 2 (PI-I). **No** son parte
del pipeline de datos canónico (ese vive en `pipeline/`: `python -m pipeline.scrape_all`,
`python -m pipeline.build_panel`, etc.). Se ejecutan **desde la raíz del
repo** para que las rutas relativas (`reports/`, `models/`, `mlflow.db`) y los
venvs (`ante/`, `ante_nf/`) resuelvan:

```bash
bash experiments/run_campaign.sh > reports/campaign.log 2>&1
ante/bin/python experiments/run_ensembles.py --mlflow
```

| Grupo | Scripts |
|---|---|
| Entrenamiento global deep | `run_global_deep.py` · `run_deep_pi.py` (`run_neuralforecast.py` está **DEPRECATED**: produjo el claim retractado; ver su docstring) |
| Mejoras / ensembles | `improve_*.py` · `run_ensembles.py` · `aggregate_seeds.py` |
| Finalistas / export | `save_finalists*.py` · `export_forecasts.py` · `eval_deep_pi.py` |
| Orquestadores (bash) | `run_rederivation.sh` (runbook completo post-cambio-de-datos) · `run_campaign.sh` · `run_experiments.sh` · `run_overnight_global.sh` · `save_finalists.sh` |
| Tracking / sync | `sync_mlflow.py` · `sync_all.sh` (el módulo `vp_data.tracking` lo comparte `vp_model`) |
