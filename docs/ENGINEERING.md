# Índice normativo de la documentación de ingeniería

Dónde vive cada decisión, quién manda sobre ella y quién la consume. Este archivo **no
republica cifras ni hechos canónicos**: para eso están las autoridades que lista, y una prueba
(`tests/test_docs_links.py`) falla si aquí se cuela un valor que pertenece a
`reports/governance/key_facts.json`.

Regla de lectura: **una decisión, una autoridad**. Si dos documentos parecen decir lo mismo, el
de la columna *Autoridad* es el que manda y el otro debe enlazarlo, no repetirlo.

## Las tres clases

- **Canónico** — se mantiene a mano y define una decisión. Cambiarlo es cambiar la decisión.
- **Generado** — lo produce un script desde fuentes canónicas. Editarlo a mano es un error:
  se regenera y el guardián o su prueba lo delatan.
- **Histórico** — registra un plan o un hallazgo con fecha. Se conserva como acta; no gobierna
  el presente y no debe citarse como norma vigente.

## Matriz: documento → clase → autoridad → consumidores

| Documento | Clase | Autoridad sobre | Consumidores verificados |
|---|---|---|---|
| [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) | canónico | el contrato de capas y la dirección de las dependencias | `tests/test_architecture.py`, [`docs/adr/0001-project-boundaries.md`](adr/0001-project-boundaries.md) |
| [`docs/FAILURE_MATRIX.md`](FAILURE_MATRIX.md) | canónico | los modos de fallo, su señal, su rollback y la prueba que los simula | este índice (sin consumidor en código: es material de revisión y de defensa) |
| [`docs/STORAGE_POLICY.md`](STORAGE_POLICY.md) | canónico | qué artefacto vive en git, en S3 o se regenera | `README.md`, el paso de tamaños del cron (`.github/workflows/freeze_and_rebuild.yml`) |
| [`docs/DEBT_BASELINE.md`](DEBT_BASELINE.md) | canónico | la mecánica del trinquete de deuda y su clasificación | `tools/check_debt.py` y su baseline [`docs/debt_baseline.json`](debt_baseline.json) |
| [`docs/dead_code_report.md`](dead_code_report.md) | histórico | el barrido de código muerto del 12-jul-2026 y sus borrados | `tests/test_public_api.py` (exige actualizarlo al retirar un símbolo) |
| [`docs/mlops_experimentos.md`](mlops_experimentos.md) | canónico | la plataforma de experimentación: record v2, sync, jerarquía de identidades y locks | `experiments/sync_mlflow.py`, `experiments/save_finalists.py`, `experiments/backfill_mlflow_legacy.py` |
| [`docs/ROADMAP.md`](ROADMAP.md) | histórico | el plan de elevación del modelo de datos (cobertura y modelado dimensional) | `README.md` |
| [`docs/MLOPS_ARCHITECTURE.md`](MLOPS_ARCHITECTURE.md) | generado | la vista única de la plataforma y su diagrama | `experiments/make_mlops_architecture.py`, guardián de consistencia, `tests/test_mlops_architecture.py` |
| [`docs/CONSISTENCY.md`](CONSISTENCY.md) | canónico | la regla #0 y cómo se hace cumplir | `tools/check_consistency.py`, `tools/consistency_rules.yml` |
| [`docs/FORECAST_EVAL.md`](FORECAST_EVAL.md) | canónico | los protocolos de evaluación y el registro prospectivo | guardián (grupo `governance`), `experiments/score_forecasts.py` |
| [`docs/PROMOTION_POLICY.md`](PROMOTION_POLICY.md) | canónico | la política pre-registrada de promoción | `vp_model/promotion.py`, `experiments/run_promotion_gate.py` |
| [`docs/DVC.md`](DVC.md) | canónico | la frontera DAG-determinista contra runner-transaccional y el hook del lock | `tools/check_dvc_lock_fresh.py`, `dvc.yaml` |
| [`docs/CLEANING.md`](CLEANING.md) | canónico | las decisiones de limpieza del panel | `vp_data/cleaning.py` |
| [`docs/THREAT_MODEL.md`](THREAT_MODEL.md) | canónico | el modelo de amenaza vigente y lo que se retiró con él | [`docs/adr/0002-supply-chain-gates-retired.md`](adr/0002-supply-chain-gates-retired.md), `tests/test_docs_links.py` |
| [`docs/data_dictionary.md`](data_dictionary.md) | canónico | el significado de cada campo del almacén | `docs/ROADMAP.md`, `docs/er_diagram.md` |
| [`docs/er_diagram.md`](er_diagram.md) | generado | el diagrama entidad-relación del esquema estrella | `experiments/make_data_figures.py`, guardián (grupo `diagrams`) |
| [`docs/experiments_inventory.json`](experiments_inventory.json) | canónico | la clase y el consumidor de cada entrypoint de `experiments/` | `tools/check_experiments_inventory.py` |
| [`docs/coverage_floors.json`](coverage_floors.json) | canónico | los pisos de cobertura por capa | `tools/check_coverage_floors.py`, paso E1 del CI |
| [`docs/model_catalog.json`](model_catalog.json) | canónico | el catálogo de modelos y su estado | `tools/check_model_catalog.py`, guardián |

## Decisiones de arquitectura (ADR)

Las decisiones con consecuencias estructurales viven como ADR numerados y contiguos; ninguno se
edita después de aceptarse: se supera con uno nuevo.

| ADR | Qué decide |
|---|---|
| [`0001-project-boundaries.md`](adr/0001-project-boundaries.md) | los límites del proyecto y sus puertos |
| [`0002-supply-chain-gates-retired.md`](adr/0002-supply-chain-gates-retired.md) | el retiro de los gates de cadena de suministro y la seguridad al mínimo viable |
| [`0003-campaign-transaction.md`](adr/0003-campaign-transaction.md) | la campaña como transacción con máquina de estados y publicación gobernada |

## Lo que este índice no gobierna

- **Cifras.** Toda cifra publicada sale de `reports/governance/key_facts.json` y la hace cumplir
  el guardián descrito en [`docs/CONSISTENCY.md`](CONSISTENCY.md).
- **Planes de trabajo.** Viven fuera del repositorio, en `Prompts/`, y no son normativos aquí.
- **Documentos del repositorio web.** El triage de dependencias npm pertenece al repo que
  contiene su gate semanal; duplicarlo aquí crearía una segunda autoridad sobre lo mismo.

## Cómo se verifica este índice

`tests/test_docs_links.py` falla cerrado ante enlaces rotos, fuentes inexistentes, un documento
consolidado sin fila o con más de una, dos filas que reclamen la misma autoridad, filas sin clase
o sin consumidores, ausencia de backlink recíproco, cifras canónicas republicadas aquí, y ADRs
sin registrar o con numeración discontinua.
