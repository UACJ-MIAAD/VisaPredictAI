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
| Modelos nuevos (épica AL) | `run_statsforecast.py` (AutoETS/AutoTheta/AutoCES/DOT, corre en `ante_nf`) · `run_global_gbm.py` (GBM GLOBAL sobre el panel apilado, receta M5) · `run_hurdle.py` (P(move)×magnitud) · `apply_cone_constraints.py` (auditoría retrospectiva del cono FAD≤DFF / país≤All-Charg. + contador de violaciones; desde F1 la proyección vive **single-source en `vp_model/cone.py`** y `generate_web_forecasts.py` la aplica a cada añada ANTES de serializar, con `cone_violations_pre/post` en el meta) |
| Tracking / sync | `sync_mlflow.py` · `sync_all.sh` (el módulo `vp_data.tracking` lo comparte `vp_model`) |

**Decisión AL7 — modelos que NO se añaden** (detalle en el docstring de
`run_statsforecast.py`): Moirai y Lag-Llama son foundation zero-shot de la misma
clase que Chronos (~0.225 MASE aquí) — añadirían filas, no insight; MSTL descompone
estacionalidades múltiples que el censo EDA descarta (0/74 series estacionales); la
reconciliación jerárquica estándar (MinT) exige series que SUMEN a agregados y las
fechas de corte son estadísticos de orden, no cantidades aditivas — las restricciones
de orden se explotan en su lugar con la proyección al cono (`vp_model/cone.py`,
incorporada al publicador; `apply_cone_constraints.py` para auditoría retrospectiva).
