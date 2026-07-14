# Plataforma de experimentación (MLflow + DVC)

Tracking de experimentos, versionado de datos/modelos y reproducibilidad para la campaña de
modelado. Diseñada alrededor de los **dos entornos** del proyecto (incompatibles por pandas):

- `ante` — pandas 3, pool local de 21 modelos (darts/torch/GBMs).
- `ante_nf` — pandas 2.3.3 + neuralforecast, deep global. **MLflow vive aquí** (exige pandas<3).

## Arquitectura

```
[ante  o  ante_nf]                    [ante_nf]
 tracking.log_run()  ──►  mlruns_staging/*.jsonl  ──►  experiments/sync_mlflow.py  ──►  mlflow.db (SQLite)
 (stdlib, env-agnóstico)   (records idempotentes)       (ingesta)          + mlartifacts/
```

`vp_data/tracking.py` es **stdlib pura** (sin mlflow ni vp_model) → corre idéntico en ambos venv y
escribe records JSONL. `experiments/sync_mlflow.py` (en `ante_nf`) los vuelca a MLflow, idempotente por
`rec_id` (re-sincronizar no duplica). MLflow 3.x deprecó el file-store → **backend SQLite**.
El sync preserva el **timestamp real** de cada corrida (`MlflowClient.create_run(start_time=ts)`,
AO4): la UI ordena por la fecha del experimento, no por la fecha del sync.

## Contrato del record v2 (A2/A6, plan auditoría 2026-07-12)

Cada línea del staging lleva `schema_version: 2`; las líneas viejas **sin** `schema_version`
se tratan como **v1** (el sync las sigue leyendo). Campos v2:

| Campo | Contenido |
|---|---|
| `experiment` / `run_name` / `params` / `metrics` / `tags` / `artifacts` / `ts` | igual que v1 (métricas no finitas se filtran; `tags` siempre incluye `git_sha`, `git_dirty`, `pipeline_run_id`) |
| `content_hash` | hash de CONTENIDO `{experiment, run_name, params, metrics}` — **idéntico al `rec_id` v1** (dos corridas con las mismas métricas lo comparten) |
| `rec_id` | **clave de EVENTO**: hash de `experiment + run_name + pipeline_run_id + data_hash + code_sha + recipe_version + seed + content_hash + ts + seq`. Dos eventos distintos jamás colisionan (`seq = pid:contador` desambigua el mismo instante) |
| `provenance` | `pipeline_run_id` · `data_hash` (sha256 del panel canónico) · `code_sha` (SHA completo) · `recipe_version` · `seed` · `env_lock_hash` (sha256 conjunto de `locks/*.txt`) · `seq` |
| `telemetry` (opcional, A6) | `status` (ok/failed) · `duration_s` · `rss_peak_mb` · `gpu_mem_mb` · `artifact_bytes` · `warnings` · `exception` tipada (`{type, message}`) |

**Fallbacks (jamás se fabrica procedencia):** `data_hash` sin panel ⇒ `unknown`;
`recipe_version`/`seed` sin kwarg ni entrada en `params` ⇒ `unknown`; `code_sha`/`env_lock_hash`
irresolubles ⇒ `unknown`.

**Escritura transaccional (A6):** `log_run` toma `fcntl.flock` exclusivo sobre el JSONL +
append + flush + `fsync` — N escritores paralelos no pierden ni mezclan registros
(`tests/test_tracking_concurrency.py`). Para campañas usar el context-manager
`vp_model.tracking.track_run(...)`: mide duración/RSS/GPU/artefactos, acumula warnings y
loguea INCLUSO si el bloque falla (status `failed` + excepción tipada, re-lanzada).

## Sync v2: idempotente, concurrent-safe y con dedup EXPLÍCITA

- El sync corre bajo lock de archivo (`mlruns_staging/.sync.lock`): dos syncs paralelos se
  serializan; re-correr sobre el mismo staging no duplica (clave = `rec_id`).
- **Deduplicación v1 explícita**: el `rec_id` v1 (hash de contenido) colapsa eventos idénticos
  en métricas pero distintos en `pipeline_run_id`/tags/ts (~1,777 líneas del staging histórico).
  Cada corrida del sync escribe `reports/governance/mlflow_sync_reconciliation.json` con los
  rec_id colapsados, sus conteos y el motivo — nada se descarta en silencio.
- Los records v2 ingieren además: procedencia como tags `vp.*`, `content_hash`,
  `schema_version`, el panel como **input dataset** (best-effort `client.log_inputs`, digest =
  `data_hash`), telemetría (duración/RSS/GPU/bytes como métricas `telemetry_*`;
  warnings/excepción como tags) y estado `FINISHED`/`FAILED` según `telemetry.status`.

## Artefactos portables + backfill de los 13,165 runs históricos

- Los **experiments nuevos** se piden con `artifact_location` RELATIVA al repo
  (`mlartifacts/{name}`); ⚠️ mlflow 3.x la **canonicaliza a absoluta** al crearla, así que
  el sync la regresa a la forma relativa con `_portabilize` (reescribe SOLO las filas
  experiment/run creadas en esa pasada; verificado contra mlflow 3.14 real) → ninguna URI
  nueva contiene `/Users/`. Correr sync y UI **desde la raíz del repo** (la ruta relativa
  se resuelve contra el cwd).
- Los **runs históricos** conservan `artifact_uri` absoluta (`file:///Users/...`). NO se
  reescriben: para leerlos, resolver el sufijo tras `mlartifacts/` contra la raíz del repo
  (solo 5 runs tienen artefactos físicos: `mlartifacts/champion_challenger/*`).
- **Backfill** (`experiments/backfill_mlflow_legacy.py`, stdlib, corre en `ante`):
  etiqueta cada run legado (sin tag `schema_version`) con `legacy_status` ∈
  {`legacy_complete` (artefactos físicos presentes), `legacy_metrics_only` (la mayoría),
  `invalid` (0 métricas — 2 runs, no cuentan como exitosos)} y repara las raíces de los 19
  experiments a rutas relativas. Idempotente; **dry-run por defecto**:

  ```bash
  ante/bin/python experiments/backfill_mlflow_legacy.py           # dry-run (reporta, no escribe)
  ante/bin/python experiments/backfill_mlflow_legacy.py --apply   # aplica (1 transacción)
  ```

- **⚠️ Impacto en `mlflow.db.dvc`**: `mlflow.db` es DVC-tracked y gitignored. Tras un
  `--apply` (o cualquier sync que ingiera runs) hay que correr `dvc commit mlflow.db.dvc`
  en el mismo commit para que el pointer refleje la db nueva.

## ⚠️ Decisión (AO9): MLflow = archivo histórico, NO dashboard en vivo

MLflow se sincroniza **manualmente** (`make mlflow-sync`, o como paso 1 de
`experiments/sync_all.sh` / `make sync`), a demanda, cuando alguien quiere comparar corridas
en la UI. **No** corre en el cron ni en CI (el staging JSONL allí es efímero). El registro
**durable y canónico** de toda cifra publicada son los CSV/JSON commiteados en git
(`reports/`) + el `git_sha` que `tracking.log_run` sella en cada record. Esto es una
decisión, no una deriva: un MLflow "en vivo" exigiría un tracking server y credenciales en
el cron a cambio de nada que el guardián de consistencia no cubra ya.

## Uso

```bash
# 1. correr un barrido con tracking (cualquier env; ejemplo pool local)
ante/bin/python -m vp_model.run_comparison --country all --table FAD --block family --mlflow

# 2. sincronizar el staging a MLflow (en ante_nf; equivale a `make mlflow-sync`)
ante_nf/bin/python experiments/sync_mlflow.py

# 3. abrir la UI para comparar corridas (filtra por params, ordena por métrica)
PYTHONPATH=tools/mlflow_shim ante_nf/bin/mlflow ui --backend-store-uri sqlite:///mlflow.db --default-artifact-root mlartifacts/ --port 5001
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
# P0R.5: DVC gobernado por la interfaz única (entorno aislado dvc-tool).
DVC="python -m tools.python_env exec --profile dvc-tool -- dvc"
$DVC add models/ data/processed/visa_panel_long.parquet data/processed/visapredict.duckdb
git add *.dvc data/processed/*.dvc .gitignore        # los punteros van a git
$DVC push                                            # sube los binarios al remote S3 (necesita creds AWS)
# en otra máquina: $DVC pull  reconstruye datos+modelos
```

Remote: `s3://visapredictai-raw-snapshots/dvc-store` (cuenta AWS del proyecto; el autor hace
`dvc push` con sus credenciales, igual que el bundle GPU).

## Guardar modelos

Modelos darts: `model.save("models/{tabla}/{modelo}_{pais}_{cat}/model.pkl")`. Modelos torch
(neuralforecast/darts-RNN): `.save()` genera `.ckpt`/`.pt`. Cada finalista se persiste, se
versiona con DVC y se loguea como artifact en MLflow → cualquier número del `.tex` tiene su
modelo recuperable.

**Decisión (AO5): `models/` es un snapshot de campaña con acta de nacimiento, no shelf-ware
mudo.** Hoy NADIE lo consume en producción — el demostrador web re-ajusta los modelos
estadísticos en cada corrida (son baratos). Se conserva porque cada entrada de
`models/manifest.jsonl` lleva `git_sha` + `git_dirty` + `panel_hash` (md5 del panel, mismo
formato de 12 hex que la model card): cualquier pickle puede rastrearse hasta el código y
los datos exactos que lo produjeron (`experiments/save_finalists.py`).

## Despliegue en sombra (AO6)

El cron refresca el veredicto campeón–retador cada boletín
(`experiments/run_champion_challenger.py`) y congela además la añada del **mejor retador**
en un ledger sombra separado e inmutable: `reports/prospective/forecast_log_shadow.csv`
(`experiments/freeze_shadow.py`, `make shadow`; `shadow=true` + receta serializada por fila).
Es un archivo APARTE a propósito: la clave de idempotencia del ledger campeón
(origin/serie/fecha, `keep="first"`) haría colisionar filas sombra y campeón y una de las
dos se perdería en silencio. El scorecard del campeón queda intacto por construcción
(`score_forecasts.py` lee solo `forecast_log.csv`); puntuar la sombra para la confirmación
prospectiva del gate de promoción es el siguiente paso natural de `score_forecasts.py`.

## Próximo (campaña F1–F3)
Hook de `tracking` en `aggregate_seeds`/`run_global_deep` (deep), matriz de variantes
(espacio-target × normalización × HPO × híbridos × ensembles), multi-semilla, y el frontier
en GPU (EC2) logueando al mismo MLflow.

## Jerarquía de identidades y locks por perfil (C3, plan auditoría 2026-07-11)

**Identidades** (cada nivel enlaza al siguiente; mismo id ⇒ misma cosa):

| Id | Vive en | Qué identifica |
|---|---|---|
| `release_id` | `reports/release/release_manifest.json` (content-addressed) | El corte publicable completo (109 artefactos con SHA-256) |
| `pipeline_run_id` | manifiesto · filas nuevas del ledger · tags de cada record JSONL (`VP_PIPELINE_RUN_ID=$GITHUB_RUN_ID` en el cron; `local` en escritorio) | La corrida del pipeline que produjo esos artefactos |
| `run_id` de `config.run_metadata()` | JSONL/MLflow por corrida de modelado | Una corrida experimental (semilla, libs, params, linaje de datos) |
| `deployment_id` | filas del ledger | El release vigente cuando se congeló la añada |
| `model_version` | filas del ledger · manifiesto (`champion_recipes`) | La receta desplegada/sombreada |

MLflow sigue siendo **histórico local-dev** (staging JSONL → `sync_mlflow`), jamás
dependencia productiva: el registro durable es el artefacto commiteado en git.

**Locks transitivos por perfil** (`make lock` → `tools/make_locks.sh`):

- `locks/runtime.txt` — venv fresco con `pip install -e .` (datos puros).
- `locks/dev.txt` — venv fresco con `.[dev]`; **el job lint-and-test de CI instala de
  este lock** (toda la cadena transitiva fijada, no solo las directas).
- `locks/model-cpu.txt` — venv FRESCO `.[dev,model]` (P0R.4; antes freeze del `ante/` mutable).
- GPU/deep — perfil **aislado** (`requirements/deep.in`, pandas 2.x): 3 locks HASHEADOS
  `locks/deep-{macos-arm64,linux-x86_64-cpu,linux-x86_64-cu126}.txt`. El bundle EC2 instala
  `-r ../locks/deep-linux-x86_64-cu126.txt` (torch 2.13.0+cu126). Sustituye al viejo
  `aws_gpu/ante_nf-requirements.lock` (borrado en P0R.4: freeze mutable con torch 2.12.0).

Sin hashes ni secretos (guard en el generador — ojo: la clase POSIX lleva `]` primero;
`\[` en grep BSD cierra la clase). Regenerar locks es un upgrade deliberado y auditado
por PR, nunca parte de un build.
