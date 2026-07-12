# Mapa de dependencias y límites (E1, plan auditoría 2026-07-11)

El contrato de capas del repo, **verificado por `tests/test_architecture.py`** (AST de
imports, no grep de substrings). La dirección solo apunta hacia abajo:

```
experiments/   entrypoints y orquestación (campañas, generadores, runners del cron)
    │  puede importar cualquiera
tools/         gates y CLIs (consistencia, contratos, publish, locks, LaTeX log)
    │  → vp_data, vp_model
vp_model/      dominio de modelado: métricas, walk-forward, intervalos, ledger,
    │          promoción, significancia, paleta        → vp_data
pipeline/      DAG ejecutable de datos (scrape offline → panel → almacén → feeds)
    │          → vp_data
vp_data/       dominio de datos: parseo, limpieza, config, contratos, tracking
               → solo stdlib + libs de terceros
```

## Puertos (I/O detrás de una sola puerta)

| Puerto | Dónde vive | Adapter / nota |
|---|---|---|
| **Red** | `vp_data.visa_common.get_soup` (retry+backoff) y `pipeline.freeze_snapshots` (el ÚNICO paso online del sistema) | Todo lo demás se prueba offline; el test lo exige |
| **Tracking** | `vp_data.tracking.log_run` → JSONL append-only en `mlruns_staging/` | MLflow es un adapter HISTÓRICO (`experiments/sync_mlflow.py`), jamás dependencia productiva |
| **Reloj** | Inyectable donde importa la evidencia: `ledger.stamp_rows(frozen_at=…)`, `tracking.log_run(ts=…)` | Los outs DVC son función pura de sus deps: cero reloj dentro (lección H3) |
| **Filesystem** | Rutas centralizadas en `vp_data.config` / `vp_model.config` | Nunca re-tipear rutas (`BULLETINS_JSON_PATH`, `PANEL_PATH`…) |
| **Git** | `vp_data.tracking.git_state`, `vp_model.ledger.git_sha` | Tolerantes a git ausente (degradan a "unknown"/"n/d") |

## Reglas que el test hace cumplir

1. **Dirección de capas**: `vp_data` no importa nada del proyecto; `pipeline` y
   `vp_model` solo `vp_data`; `tools` no importa `experiments`.
2. **Red solo en sus puertos**: ningún otro módulo de dominio importa
   `requests`/`urllib`/`http`/`socket`.
3. **Dominio sin MLflow/DVC**: reglas de visas, métricas y postproceso se importan y
   prueban sin ninguno de los dos (el DAG los orquesta desde fuera).

## Fronteras del workspace (ADR-0001)

Las fronteras del proyecto completo — este mapa de capas, el porqué de **dos repos +
una superficie documental** (no "tres repos" ni monorepo), el contrato del repo web
(consume EXCLUSIVAMENTE releases content-addressed vía `release_manifest.json` +
verificación SHA-256; jamás importa código) y el estado PROPUESTA de la migración a
`src/visapredictai` (US B2) con sus prerequisitos — están decididas y justificadas en
**[`docs/adr/0001-project-boundaries.md`](adr/0001-project-boundaries.md)**. Ese ADR
es la fuente de verdad de las fronteras; este documento solo detalla el mapa de capas
y los puertos del repo de datos. La regla "ningún paquete de dominio nuevo en la
raíz" la hace cumplir el whitelist de `tools/validate_structure.sh` (`make validate`,
CI); el ADR la registra, el test de arquitectura la referencia sin duplicarla.

## Decisiones deliberadas (no "faltantes")

- **Adaptación de CCDS, no conformidad (I1):** la estructura está *inspirada en
  Cookiecutter Data Science v2* (que fomenta adaptar), con desviaciones deliberadas:
  `src/` único → **dos paquetes** con dirección de capas (`vp_data`/`vp_model`) +
  `pipeline/` ejecutable; `notebooks/` no existe (todo es script reproducible);
  `models/` binarios van por DVC→S3 (`models.dvc`), no en git; `data/external|interim`
  → `snapshots/raw/processed` propios del dominio; `references/` → `docs/` + `reports/`
  por rol. `tools/validate_structure.sh` certifica ESTE contrato, no la plantilla.
- **Sin framework DI** (aceptación E1): las costuras son parámetros con default
  (`stamp_rows(vintage=…, phash=…)`, `check(root=…, contracts_dir=…)`) — suficientes
  para pruebas herméticas, cero ceremonia.
- **`experiments/` es la capa de composición**: ahí se permite acoplar todo (runners,
  campañas, sync_mlflow); el inventario/clasificación fino de sus entrypoints es I2.
- Frontera DAG-determinista vs runner-transaccional: `docs/DVC.md` (C1/C2).
- Jerarquía de identidades y locks por perfil: `docs/mlops_experimentos.md` (C3).
