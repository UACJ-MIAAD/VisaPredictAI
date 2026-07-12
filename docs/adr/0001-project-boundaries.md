# ADR 0001 — Fronteras del workspace: capas, dos repos y una superficie documental

- **Estado:** Aceptada (fronteras vigentes) · la migración a `src/visapredictai` (§Decisión 4) queda en **PROPUESTA**.
- **Fecha:** 2026-07-12
- **Origen:** US B1 del `Prompts/PLAN_AUDITORIA_TRES_REPOS_MLOPS_CLEAN_CODE_2026-07-12.md` (repo académico hermano).
- **Verificación mecánica:** `tests/test_architecture.py` (dirección de imports por AST) + `tools/validate_structure.sh` (whitelist cerrado de la raíz) + gate de release del cron (deploy solo tras CI verde del SHA exacto).

## Contexto

El proyecto VisaPredict AI se compone hoy de **dos repositorios Git** y **una superficie
documental versionada dentro del primero**:

| Superficie | Dónde vive | Qué es |
|---|---|---|
| Datos + modelado | `VisaPredictAI` (este repo) | Scraping→panel→almacén→forecasts→gobernanza; Python 3.14, DVC, DuckDB, cron de boletines |
| Web / demostrador | `VisaPredictAI_web` (repo hermano) | Next.js 15 export estático en Netlify, bilingüe, VisaBot RAG |
| Documental | `reports/latex/` + `reports/paper_micai/` (dentro de ESTE repo) | Anteproyecto, entregable PI-I y paper MICAI; Overleaf compila desde GitHub |

El plan de auditoría se tituló "TRES_REPOS", pero **no existe un tercer repositorio**:
el árbol real son dos repos y la superficie documental de arriba. Dentro del repo de
datos, el código vive en cuatro paquetes-capa de raíz (`vp_data/`, `pipeline/`,
`vp_model/`, `experiments/`) más `tools/`. Dos riesgos motivan este ADR:

1. **Monorepo accidental**: que el web empiece a importar código o a leer artefactos
   intermedios del repo de datos (o viceversa), acoplando toolchains y cadencias de
   deploy que hoy son independientes.
2. **Migración cosmética**: que la reorganización a `src/visapredictai` (US B2) mueva
   archivos sin preservar la dirección de dependencias, sin compatibilidad y sin
   pruebas de equivalencia, rompiendo el DAG de DVC y el cron en el camino.

## Decisión

### 1. Diagrama de dependencias permitido (capas del repo de datos)

La dirección solo apunta hacia abajo; en términos del plan: **data ← modeling ←
workflows**, y el web solo consume releases.

```
experiments/   workflows/runners (campañas, generadores, orquestación del cron)
    │  puede importar CUALQUIER capa (es la capa de composición)
tools/         gates y CLIs (consistencia, contratos, estructura, locks)
    │  puede importar las capas de dominio (vp_data, vp_model, pipeline);
    │  PROHIBIDO importar experiments
vp_model/      dominio de modelado (métricas, walk-forward, ledger, promoción)
    │  → solo vp_data
pipeline/      DAG ejecutable de datos (scrape offline → panel → almacén → feeds)
    │  → solo vp_data
vp_data/       núcleo de datos importable (parseo, limpieza, config, tracking)
    │  → solo stdlib + libs de terceros
```

Reglas equivalentes en negativo (las que el test codifica):

- `vp_data` **no** importa `vp_model`, `pipeline`, `experiments` ni `tools`.
- `pipeline` **no** importa `vp_model`, `experiments` ni `tools`.
- `vp_model` **no** importa `pipeline`, `experiments` ni `tools`.
- `tools` **no** importa `experiments`.

Estas reglas están **codificadas** en `tests/test_architecture.py`
(`test_import_direction_between_layers`, análisis AST de imports) y corren en CI con
la suite normal: un import invertido rompe el build. Los puertos de I/O (red, reloj,
tracking, filesystem, git) se documentan en `docs/ARCHITECTURE.md` y tienen sus
propios tests en el mismo archivo.

**Ningún paquete de dominio nuevo en la raíz.** La raíz es un whitelist cerrado en
`tools/validate_structure.sh` (v2, `make validate`, encadenado a `make check` y CI):
cualquier directorio o `.py` nuevo en la raíz falla el gate. Un dominio nuevo entra
como submódulo de una capa existente o pasa por un ADR que actualice este mapa y el
whitelist en el mismo commit. El test de arquitectura **referencia** este gate, no lo
duplica (una sola fuente de verdad por regla).

### 2. El repo web consume EXCLUSIVAMENTE releases content-addressed

`VisaPredictAI_web` **jamás importa código Python** de este repo ni lee artefactos
intermedios. Su única interfaz es el **release manifest**:

- **Productor** (este repo): `experiments/build_release_manifest.py` emite
  `reports/release/release_manifest.json` — la lista completa de artefactos de un
  corte publicable, cada uno con SHA-256, tamaño, MIME y criticidad
  (`critical`/`required`/`optional`), bajo un `release_id` determinista derivado del
  contenido (mismos bytes ⇒ mismo id).
- **Consumidor** (repo web): `scripts/fetch-data.mjs` + `lib/release.mjs`
  (`MANIFEST_PATH = "reports/release/release_manifest.json"`) descargan a staging,
  verifican **cada** artefacto contra su SHA-256/tamaño y hacen **swap atómico**: o
  entra el corte completo del MISMO `release_id`, o el sitio conserva el corte
  anterior íntegro (estado `stale`). El resultado se publica en
  `/data/release-state.json` (provenance del corte servido).

Consecuencia operativa: los dos repos solo se comunican por **bytes verificados**.
Cambiar el contrato (añadir/quitar un artefacto consumido) exige tocar el productor
del manifiesto y el consumidor del web en la misma sesión (regla #0 del proyecto), y
el gate de release del cron bloquea el deploy hasta que el CI del SHA exacto está
verde.

### 3. Por qué DOS repos + una superficie documental (y no "tres repos" ni monorepo)

**No hay tercer repo para los documentos, deliberadamente.** El anteproyecto, el
entregable PI-I y el paper MICAI viven en `reports/latex/` y `reports/paper_micai/`
de ESTE repo porque sus cifras insignia son **artefactos derivados del pipeline**:
macros `\factXxx` (`key_facts.tex`, `fe_facts.tex` generados por
`experiments/build_key_facts.py`), tablas generadas por `experiments/make_tex_tables.py`
y figuras de `experiments/make_*_figures.py`. La regla #0 (mismo número en código,
docs, web y RAG) solo es exigible mecánicamente si la prosa y sus generadores
versionan **en el mismo árbol y el mismo commit** — es lo que escanea
`tools/check_consistency.py`. Un tercer repo rompería esa atomicidad: cada
re-derivación de cifras necesitaría un commit coordinado cross-repo sin gate que lo
ate. Overleaf compila directamente del GitHub de este repo (sync bidireccional), lo
que también exige que el `.tex` viva aquí.

**No monorepo con el web.** Mantener `VisaPredictAI_web` como repo separado es
decisión afirmativa, no inercia:

- Toolchains y cadencias disjuntos: Python/DVC/cron de boletines vs Node/Next.js/
  deploy continuo en Netlify. Un monorepo acoplaría CI y locks de dependencias sin
  beneficio (el plan lo registra: "Contrato por release; no monorepo sin beneficio").
- El contrato por release (§2) ya da una garantía **más fuerte** que compartir
  código: la frontera es verificable por hash y fail-closed. En un monorepo la
  tentación de importar "solo esta función" cruzaría la frontera sin gate.
- El aislamiento limita el radio de daño: un fallo del bloque de modelado no puede
  romper el build del web (que degrada a su corte anterior), y viceversa.

### 4. Migración a `src/visapredictai` (US B2) — estado: PROPUESTA

La consolidación de los paquetes de raíz en un único paquete instalable bajo `src/`
**no está aceptada aún**; se registra aquí para que, si se ejecuta, no sea cosmética.
Mapa objetivo (del plan, épica B):

| Hoy | Destino propuesto |
|---|---|
| `vp_data/` | `src/visapredictai/data` |
| `vp_model/` | `src/visapredictai/modeling` |
| `pipeline/` | `src/visapredictai/pipelines` |
| `experiments/` (runners productivos) | `src/visapredictai/workflows` |
| `experiments/` (research / one-shots) | `scripts/research` / `archive/` (US B3) |

**Prerequisitos obligatorios (criterios de aceptación de B2):**

1. **Shims de compatibilidad durante dos releases**: `vp_data`, `vp_model` y
   `pipeline` siguen importables con `DeprecationWarning`; nada externo rompe el día
   del move.
2. **Characterization tests por capa ANTES de mover**: los outputs del DAG
   (panel, almacén, facts, forecasts) quedan byte-idénticos; la migración no puede
   cambiar resultados.
3. **No big-bang**: mover por capability (una capa/capacidad por commit verificable),
   con imports internos absolutos, `dvc.yaml`/`dvc.lock` y el cron verdes en el MISMO
   commit de cada movimiento.
4. **Independencia del cwd**: editable install y wheel importan desde cualquier
   directorio (la razón de ser de `src/`).
5. **La dirección de capas de §1 se conserva idéntica** bajo los nombres nuevos
   (`data ← modeling ← workflows`); `tests/test_architecture.py` se actualiza en el
   mismo commit para vigilar los paths nuevos, nunca se desactiva.

Hasta que B2 se acepte y ejecute con esos prerequisitos, la estructura vigente es la
de §1 y el whitelist de raíz la protege.

## Consecuencias

**Positivas**

- Un import invertido o un paquete de dominio nuevo en la raíz **rompe CI** (test AST
  + whitelist), no depende de revisión humana.
- El web no puede consumir bytes no verificados: la frontera repo↔repo es
  content-addressed y fail-closed; una mezcla de añadas es imposible por
  construcción (swap atómico).
- La migración B2 no puede ejecutarse como "mover carpetas": sus prerequisitos
  (shims, characterization, no big-bang) quedan registrados y son auditables.
- Los documentos académicos heredan la trazabilidad del pipeline (macros y tablas
  derivadas) sin coordinar commits entre repos.

**Costos asumidos**

- Dos repos implican propagación cross-repo cuando cambia una cifra canónica
  (regla #0); se mitiga con `tools/check_consistency.py`, que escanea también
  componentes del web.
- Durante la eventual B2, los shims añaden superficie temporal (dos nombres para el
  mismo módulo) durante dos releases.
- `experiments/` como capa de composición admite acoplarlo todo; su inventario y
  clasificación fina (producción vs research vs archivo) es trabajo de US B3, no de
  este ADR.

**Dónde vive cada regla (enforcement)**

| Regla | Mecanismo | Archivo |
|---|---|---|
| Dirección de imports entre capas | test AST en CI | `tests/test_architecture.py` |
| Red/MLflow/DVC solo en sus puertos | test AST en CI | `tests/test_architecture.py` |
| Ningún paquete/`.py` nuevo en raíz | whitelist cerrado (`make validate`, CI) | `tools/validate_structure.sh` |
| Web consume solo releases verificados | manifiesto + verificación SHA-256 + swap atómico | `experiments/build_release_manifest.py` · web `scripts/fetch-data.mjs`, `lib/release.mjs` |
| Cifras alineadas cross-repo | guardián de consistencia (CI + pre-push) | `tools/check_consistency.py` |
| Deploy solo tras CI verde del SHA exacto | release gate del cron | `.github/workflows/freeze_and_rebuild.yml` |

## Referencias

- `docs/ARCHITECTURE.md` — mapa de capas y puertos (enlaza este ADR; no duplica).
- `Prompts/PLAN_AUDITORIA_TRES_REPOS_MLOPS_CLEAN_CODE_2026-07-12.md` (repo académico) — US B1 (este ADR), B2 (migración `src/`), B3 (clasificar `experiments/`), B4 (contrato de estructura).
- `docs/DVC.md` — frontera DAG-determinista vs runner-transaccional.
- `docs/mlops_experimentos.md` — jerarquía de identidades y locks por perfil.
