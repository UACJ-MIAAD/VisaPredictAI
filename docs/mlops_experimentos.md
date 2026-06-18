# Plataforma de experimentación (MLflow + DVC)

Tracking de experimentos, versionado de datos/modelos y reproducibilidad para la campaña de
modelado. Diseñada alrededor de los **dos entornos** del proyecto (incompatibles por pandas):

- `ante` — pandas 3, pool local de 21 modelos (darts/torch/GBMs).
- `ante_nf` — pandas 2.3.3 + neuralforecast, deep global. **MLflow vive aquí** (exige pandas<3).

## Arquitectura

```
[ante  o  ante_nf]                    [ante_nf]
 tracking.log_run()  ──►  mlruns_staging/*.jsonl  ──►  sync_mlflow.py  ──►  mlflow.db (SQLite)
 (stdlib, env-agnóstico)   (records idempotentes)       (ingesta)          + mlartifacts/
```

`tracking.py` es **stdlib pura** (sin mlflow ni vp_model) → corre idéntico en ambos venv y
escribe records JSONL. `sync_mlflow.py` (en `ante_nf`) los vuelca a MLflow, idempotente por
`rec_id` (re-sincronizar no duplica). MLflow 3.x deprecó el file-store → **backend SQLite**.

## Uso

```bash
# 1. correr un barrido con tracking (cualquier env; ejemplo pool local)
ante/bin/python -m vp_model.run_comparison --country all --table FAD --block family --mlflow

# 2. sincronizar el staging a MLflow (en ante_nf)
ante_nf/bin/python sync_mlflow.py

# 3. abrir la UI para comparar corridas (filtra por params, ordena por métrica)
ante_nf/bin/mlflow ui --backend-store-uri sqlite:///mlflow.db --default-artifact-root mlartifacts/
#   -> http://127.0.0.1:5000
```

Cada run loguea: `params` (model, country, category, table, block, semilla, hiperparámetros),
`metrics` (sel/hold MASE, sMAPE, MAE, MSIS, cobertura, CRPS), `tags` (git_sha + dirty),
`artifacts` (CSV de forecasts + modelo).

## Versionado con DVC (datos + modelos)

El **CSV abierto** (`visa_panel_long.csv`) sigue en git (fuente de verdad legible). Los
**binarios regenerables/grandes** se versionan con DVC: panel parquet, almacén DuckDB y los
**modelos entrenados** (`models/`). Pointers `.dvc` en git; binarios en el remote S3.

```bash
dvc add models/ data/processed/visa_panel_long.parquet data/processed/visapredict.duckdb
git add *.dvc data/processed/*.dvc .gitignore        # los punteros van a git
dvc push                                              # sube los binarios al remote S3 (necesita creds AWS)
# en otra máquina: dvc pull  reconstruye datos+modelos
```

Remote: `s3://visapredictai-raw-snapshots/dvc-store` (cuenta AWS del proyecto; el autor hace
`dvc push` con sus credenciales, igual que el bundle GPU).

## Guardar modelos

Modelos darts: `model.save("models/{tabla}/{modelo}_{pais}_{cat}/model.pkl")`. Modelos torch
(neuralforecast/darts-RNN): `.save()` genera `.ckpt`/`.pt`. Cada finalista se persiste, se
versiona con DVC y se loguea como artifact en MLflow → cualquier número del `.tex` tiene su
modelo recuperable.

## Próximo (campaña F1–F3)
Hook de `tracking` en `aggregate_seeds`/`run_global_deep` (deep), matriz de variantes
(espacio-target × normalización × HPO × híbridos × ensembles), multi-semilla, y el frontier
en GPU (EC2) logueando al mismo MLflow.
