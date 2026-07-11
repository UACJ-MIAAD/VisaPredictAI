# Deuda técnica: baseline, trinquete y clasificación de warnings (E3)

**Mecánica:** `tools/check_debt.py` (gate en `make check` y en el job consistency de CI)
compara los marcadores de deuda contra `docs/debt_baseline.json`. **Ningún conteo puede
subir**; si baja, el gate pide actualizar la baseline en el mismo PR (`--update`,
decisión visible). Capas contadas: `vp_data`, `pipeline`, `vp_model`, `tools`,
`experiments`.

## Política de `except Exception`

Todo `except Exception` amplio lleva **`noqa: BLE001` + comentario de continuidad**
(qué se degrada y por qué no debe abortar). Al sembrar la baseline (2026-07-11):
36 totales, **9 sin razón declarada** — todos en la familia de scrapers
(`vp_data/visa_common.py`, `pipeline/scrape_*.py`): son el patrón fail-soft por-mes del
parseo (un boletín malformado no tumba los 296) y les falta el comentario, no la
política. El trinquete impide que aparezcan nuevos sin justificar; anotar esos 9 es
limpieza incremental que BAJA la baseline.

## Clasificación de los warnings de la suite (aceptación E3)

Los ~53 warnings restantes de la suite (eran 71: las 18 `ResourceWarning` de sqlite se
ELIMINARON — engines de Optuna sin dispose, fix `df6dff4`) son repeticiones de 9
orígenes, **todos de librerías en situaciones numéricas esperadas** — ninguno por mal
uso propio:

| Tipo | Origen | Clasificación |
|---|---|---|
| `ExperimentalWarning` ×2 | Optuna TPE `multivariate`/`group` (`tune.py`) | **Esperado y deliberado** (AK4: sampler agrupado; documentado) |
| `ConvergenceWarning` | statsmodels SARIMA en series duras del stress test | **Esperado** (el runner declara/omite series que no convergen — política SARIMA-DFF) |
| `ConstantInputWarning` | scipy Spearman en `rank_check` con top-K degenerado | **Esperado** (rho indefinida cuando el objetivo empata; el CSV lo registra como NaN) |
| `UserWarning` ×5 orígenes | darts/statsmodels (frecuencias, features) en fixtures mínimas | **Esperado** (fixtures deliberadamente cortas) |

Ninguno se silencia globalmente: siguen visibles en cada corrida para que un warning
NUEVO (de origen propio) no se camufle entre los esperados.

## Baseline vigente

Ver `docs/debt_baseline.json` (versionada; la actualiza `check_debt.py --update` como
acto explícito de PR, jamás un build).
