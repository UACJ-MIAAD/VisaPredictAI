# AUDITORÍA MLOps — VisaPredictAI

_Evaluación de madurez de ingeniería del pipeline de datos del proyecto VisaPredict&nbsp;AI._
_Etapa del proyecto: **ingeniería de datos completada** (Objetivo&nbsp;1); el modelado entra el próximo semestre. Este audit evalúa la disciplina MLOps alrededor del pipeline de datos y su preparación para la fase de modelado._

---

## Resumen ejecutivo

| # | Dimensión | Nivel (0–5) | Estado |
|---|---|:--:|---|
| 1 | Control de versiones & estructura | **4** | Bueno |
| 2 | Gestión de dependencias & reproducibilidad | **1** | 🔴 Frágil |
| 3 | Pruebas automatizadas | **2** | 🟡 Mejorado en este audit |
| 4 | CI/CD & quality gates | **2** | 🟡 Sin gate de validación |
| 5 | Validación de datos & contratos | **3** | 🟡 Scripts existen, no en CI |
| 6 | Versionado de datos & artefactos | **1** | 🔴 Bloat de git |
| 7 | Observabilidad, monitoreo & alertas | **2** | 🟡 Retry sí, alertas no |
| 8 | Orquestación | **3** | Adecuado |
| 9 | Linaje & procedencia | **3** | Bueno a nivel celda |
| 10 | Seguridad & secretos | **4** | Bueno |
| 11 | Documentación | **4** | Bueno |
| 12 | Preparación para modelado | **4** | Listo |

**Veredicto: madurez global ≈ 2.4 / 5 — «Nivel 1–2: el *artefacto* es excelente, el *andamiaje* de ingeniería es inmaduro».** El panel de datos es de alta calidad (mega-audit APTO), pero la disciplina que lo rodea (reproducibilidad pin-eada, gates de CI, versionado de datos, alertas) es frágil. Un cambio de dependencia o un fallo de parseo puede degradar el dato sin que nadie se entere.

---

## Hallazgos por severidad

### 🔴 Críticos

- **C1 · Reproducibilidad rota dev↔CI.** El entorno de desarrollo corre **Python 3.14 + pandas 3.0.0**, pero el GitHub Action corre **Python 3.11 + `pandas>=2.2.2`** (instala pandas 2.x). El badge del README dice «3.10+». Tres entornos distintos para un parser sensible a comportamiento de pandas (p.ej. `to_datetime(format='mixed')` cambió entre 2.x y 3.0). **Riesgo:** el CSV que produce el CI puede diferir del de dev. **Fix:** fijar una sola versión de Python y pin-ear dependencias con cotas (`pandas>=2.2,<3.1`) o un `lock` reproducible en la versión elegida.

- **C2 · Dependencias sin fijar.** `requirements.txt` usa `>=` sin cota superior ni lockfile. Un bump menor de `beautifulsoup4`/`pandas`/`requests` puede alterar el parseo silenciosamente. **Fix:** lockfile (`pip freeze`) o cotas explícitas, regeneradas de forma controlada.

- **C3 · Bloat de versionado de datos.** Los 10 CSV + el panel + **9 PNG binarios** se commitean a `main` en cada corrida diaria. `.git` ya pesa **120&nbsp;MB** con 162 commits; los PNG cambian en cada render y son el principal motor de crecimiento. A un año de commits diarios el repo se vuelve inmanejable. **Fix:** (a) dejar de versionar `figures/` (regenerarlas on-demand o publicarlas como artefacto/Release), (b) considerar Git&nbsp;LFS o DVC para los CSV, o (c) `squash`/retención del histórico de datos.

### 🟡 Importantes

- **I1 · Sin gate de validación en CI.** El Action corre scrapers → `build_panel` → visualizadores → **commit a `main` directo**, sin ejecutar `audit_data_quality.py`, `mega_audit.py` ni los tests. Una regresión de parseo se publica sin control. **Fix:** insertar un paso que corra `tests/test_panel_integrity.py` (invariantes duras) **antes** del commit; si falla, abortar.
- **I2 · Sin sentinela de detección de drift.** Nada vigila que el nº de filas, el rango temporal o el % de estado&nbsp;F se mantengan estables corrida a corrida. Un cambio de formato HTML upstream que vacíe una serie pasaría inadvertido. **Fix:** gate de conteo (`test_min_rows` ya incluido) + comparación contra la corrida anterior.
- **I3 · Sin alertas en fallo.** El retry+reporte añadido evita pérdidas silenciosas dentro de la corrida, pero un fallo del Action sólo es visible en la pestaña Actions. **Fix:** notificación (email/issue automático) en `failure()`.
- **I4 · Footgun del centinela `NA` (corregido en este audit).** El string `"NA"` colisiona con la coerción por defecto de pandas (`pd.read_csv` lo lee como `NaN`), borrando silenciosamente la anotación de estado al reconstruir el panel — justo lo que el fix&nbsp;H1/H5 buscaba preservar. **Resuelto:** renombrado a **`UNK`**, que ningún `read_csv` ingenuo coerciona; cualquier consumidor downstream (modelado, web) lee el estado correctamente.

### 🔵 Mejoras

- **M1 · Linaje a nivel fila.** `raw_value` preserva la celda cruda (excelente), pero no hay `scrape_date` ni `source_url` por fila. **Fix:** añadir timestamp de scrape y URL del boletín para procedencia completa.
- **M2 · Contrato de esquema explícito.** El esquema vive en `CLAUDE.md` (prosa). **Fix:** un `schema.json`/contrato versionado que los tests validen.
- **M3 · Empaquetado.** Sin `pyproject.toml`/`Makefile`; los comandos viven en `CLAUDE.md`. **Fix opcional:** un `Makefile` (`make scrape`, `make audit`, `make test`) para reproducibilidad de un comando.
- **M4 · Pin de acciones por SHA.** `actions/checkout@v4` y `setup-python@v5` están pin-eadas por major (aceptable); pin por SHA es la práctica de máxima seguridad de supply-chain.

---

## Lo que YA está bien (no regresar)

- **Código modular y legible**, funciones puras testeables (`classify_status`, `classify_eb_category`, `classify_family_category`).
- **`CLAUDE.md` excelente** como documentación viva + `README`.
- **Robustez de red:** `get_soup` con retry+backoff+timeout+`raise_for_status`; `main()` reporta meses perdidos.
- **Idempotencia:** `build_panel.py` es determinista dado los CSV; clave única garantizada por dedup.
- **Validación de datos rica:** `audit_data_quality.py` + `mega_audit.py` (12 dimensiones) — sólo falta **engancharlos al CI**.
- **Seguridad:** sin secretos en el repo, permisos del Action acotados a `contents: write`.
- **Datos listos para modelar:** panel limpio, columna `status` para entrenar sólo sobre&nbsp;F, `days_since_base` como objetivo, retrogresiones preservadas.

---

## Roadmap de remediación (prioridad por impacto/esfuerzo)

| Prioridad | Acción | Esfuerzo | Aborda |
|:--:|---|:--:|---|
| 1 | Alinear Python (dev=CI) + pin-ear dependencias con cotas | Bajo | C1, C2 |
| 2 | Gate de CI: correr `tests/` antes del commit; abortar si falla | Bajo | I1, I2 |
| 3 | Dejar de versionar `figures/` (o LFS/Release) | Medio | C3 |
| 4 | Notificación en fallo del Action | Bajo | I3 |
| 5 | `scrape_date` + `source_url` por fila | Bajo | M1 |
| 6 | `schema.json` + `Makefile` | Medio | M2, M3 |

**Hecho en este audit:** suite de pruebas (`tests/test_parsers.py` 12 casos + `tests/test_panel_integrity.py` invariantes), centinela `NA→UNK` (I4), y este reporte.

---

_Generado el 14-jun-2026. El detalle de calidad del dato vive en `mega_audit_report.md`; la madurez de ingeniería, aquí._
