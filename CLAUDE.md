# CLAUDE.md — VisaPredict AI

## Proyecto y contexto de tesis

Este repositorio extrae datos históricos del **Visa Bulletin** del Departamento de Estado de EE.UU. mediante web scraping. Sirve como la base de datos para la tesis **"VisaPredict AI"** — predicción de fechas de boletines de visa con Machine Learning.

- **Autor:** Javier Rebull (al263483)
- **Programa:** Maestría en Inteligencia Artificial y Analítica de Datos (MIAAD), UACJ
- **Asesor:** Dr. Vicente García Jiménez
- **Repositorio:** https://github.com/UACJ-MIAAD/VisaBulletinScraping (ya **no** usa nada del fork original; créditos retirados 14-jun-2026)
- **Fuente de datos:** https://travel.state.gov/content/travel/en/legal/visa-law0/visa-bulletin.html

## Estructura del repositorio

```
VisaBulletinScraping/
├── CLAUDE.md                          # Este archivo (contexto para Claude Code)
├── README.md                          # Documentación original del repo
├── requirements.txt                   # Dependencias Python
├── visa_common.py                     # ★ helpers compartidos (fetch/links/fecha/estado) — NO duplicar en los scrapers
├── scrape_visa_bulletins.py           # scraper empleo (importa de visa_common; ~2 min)
├── scrape_family_visa_bulletins.py    # scraper familiar (importa de visa_common)
├── build_panel.py · audit_data_quality.py · mega_audit.py  # consolidación + auditorías
├── tests/                             # test_parsers.py (12) · test_panel_integrity.py (10 invariantes)
├── *_audit_report.md                  # data_quality · mega · mlops · solid_clean
├── visualize_visa_wait_times.py       # Generación de gráficas por país
├── data/                              # CSVs generados por el scraper
│   ├── china_visa_backlog_timecourse.csv
│   ├── india_visa_backlog_timecourse.csv
│   ├── mexico_visa_backlog_timecourse.csv
│   ├── philippines_visa_backlog_timecourse.csv
│   └── row_visa_backlog_timecourse.csv
├── figures/                           # Gráficas PNG generadas
│   ├── China_visa_wait_times.png
│   ├── India_visa_wait_times.png
│   ├── Mexico_visa_wait_times.png
│   ├── Philippines_visa_wait_times.png
│   └── RoW_visa_wait_times.png
├── ante/                              # Ambiente virtual Python (no versionar)
└── .github/workflows/
    └── update_graphs.yml — Action diaria (scrape→panel→gate→commit; ci.yml = lint+test en push)
```

## Stack técnico

- **Python:** 3.14 (macOS Apple Silicon)
- **Ambiente virtual:** `ante` (activar con `source ante/bin/activate`)
- **Ubicación local:** `/Users/haowei/Documents/Anteproyecto/VisaBulletinScraping`

### Dependencias (requirements.txt)

| Paquete         | Uso                                        |
|-----------------|---------------------------------------------|
| beautifulsoup4  | Parsing HTML del Visa Bulletin              |
| requests        | HTTP requests a travel.state.gov            |
| pandas          | Manipulación de DataFrames y CSVs           |
| matplotlib      | Generación de gráficas                      |
| tqdm            | Barras de progreso durante el scraping      |

## Tooling MLOps (mejores prácticas)

- **Dependencias pin-eadas** (`requirements.txt` + `pyproject.toml`): versiones exactas validadas en dev (pandas 3.0.0, py3.14); el Action usa el mismo Python → CI reproduce dev.
- **`ruff`** (lint + **format**) + **`mypy`** + **`pytest` con coverage gate (`fail_under=65`)** configurados en `pyproject.toml`. `make check` = lint + format-check + typecheck + test. Los tests corren vía `pytest` (con cobertura) **y** como scripts planos (`python tests/x.py`, salida 0/1) — el Action diario usa los planos (sin dep de pytest); CI y `make test` usan pytest. **Prácticas adaptadas de EpiForecast-MX** (14-jun-2026): pytest+coverage, `ruff format`, pre-commit endurecido (`check-added-large-files`, eof, yaml/toml, debug-statements), `.python-version`, CI con `concurrency`+cache.
- **Dos workflows de GitHub Actions:** `ci.yml` (lint + tests en cada push/PR a `main`) y `update_graphs.yml` (cron diario: scrape→panel→**gate de tests**→figuras→commit; abre issue `scrape-failure` en fallo).
- **`Makefile`**: `make install|scrape|panel|test|lint|figures|audit|all` (un comando). Override: `make test PY=python`.
- **`.pre-commit-config.yaml`**: ruff + tests rápidos antes de cada commit (`pre-commit install`).
- **DVC** inicializado pero **NO versiona los CSV abiertos** (son el entregable, se quedan en git; `.dvcignore` los protege). Reservado para artefactos de modelo/binarios grandes del **próximo semestre** (como EpiForecast usa `models.dvc`/checkpoints). Ver `DVC.md`.
- **`tests/`** corre sin pytest (salida 0/1): `test_parsers` (12) + `test_extraction` (6, offline sobre fixtures) + `test_panel_integrity` (10 invariantes/contrato, incl. completitud de meses).

## Comandos clave

```bash
# Todo de un comando
make test        # gate completo · make lint · make panel · make all

# Activar ambiente virtual
source ante/bin/activate

# Instalar dependencias
pip install -r requirements.txt

# Ejecutar scrapers (genera CSVs por país en data/, ~2 min c/u)
python scrape_visa_bulletins.py          # empleo (EB 1-4, FAD+DFF)
python scrape_family_visa_bulletins.py   # familiar (F1-F4, FAD+DFF)

# Consolidar el panel largo y_{p,c,b,t} -> data/visa_panel_long.csv
python build_panel.py

# Auditoría de calidad -> data_quality_report.md
python audit_data_quality.py

# MEGA AUDIT exhaustivo (12 dimensiones) -> mega_audit_report.md
python mega_audit.py

# Suite de pruebas (sin pytest; salida 0/1; corre como GATE de CI antes del commit)
python tests/test_parsers.py          # 12 casos · funciones de parseo/clasificación
python tests/test_extraction.py       # 6 casos · extracción OFFLINE sobre fixtures HTML (sin red)
python tests/test_panel_integrity.py  # 10 invariantes duras del panel (contrato + completitud de meses)

# Audit MLOps de madurez de ingeniería -> mlops_audit_report.md (estático, no regenera)

# Generar gráficas (genera PNGs en figures/)
python visualize_visa_wait_times.py
```

> El GitHub Action (`update_graphs.yml`) corre los 2 scrapers → `build_panel.py`
> → **gate de tests** → commit diario (vía `git add -A`; abre issue en fallo). Las
> figuras **ya no se generan ni versionan** en el Action (regenerar con `make figures`).

## Contexto del scraping

El scraper extrae tablas **Employment-Based** del Visa Bulletin mensual publicado por el Departamento de Estado de EE.UU.

### Qué extrae

- **Tablas:** Final Action Dates (FAD) **y** Dates for Filing (DFF) — las dos
  tablas employment-based de cada boletín, etiquetadas en `table_type`. DFF de
  empleo existe desde Oct-2015 (antes solo hay FAD).
- **Categorías:** EB-1..EB-4 + subcategorías, vía `classify_eb_category()` que
  normaliza 20 años de deriva de etiquetas a 16 códigos canónicos: `EB1` `EB2`
  `EB3` `EB3_OW` `EB4` `EB4_RW` `EB4_TRANS` `EB5` `EB5_TEA` `EB5_PILOT` `EB5_RC`
  `EB5_NONRC` `EB5_UNRESERVED` `EB5_RURAL` `EB5_HIGHUNEMP` `EB5_INFRA`. Schedule A
  Workers queda fuera de alcance (no es preferencia EB-1..5). El `EB_level` del
  CSV ahora guarda el código canónico (no el entero 1-4).
- **Países con límites especiales:** India, China, México, Filipinas
- **Resto del mundo (RoW):** "All chargeability areas except those listed"
- **Rango temporal:** Desde **Dic 2001** (piso de la fuente oficial) hasta el
  boletín más reciente. La detección de columnas es robusta a 20 años de deriva
  de formato (categoría = columna 0; país por nombre normalizado), lo que
  recuperó 2001-2003 y arregló RoW (antes truncado a 2016). **China desde
  2005-04** (antes no tenía columna EB propia). Pre-2002 da 404; 1992 solo sería
  alcanzable vía Wayback Machine (fuera de alcance).

### Estructura de los CSVs por país

| Columna              | Tipo     | Descripción                                           |
|----------------------|----------|-------------------------------------------------------|
| `EB_level`/`F_level` | str      | Categoría (empleo: código canónico EB1..EB5_*; familiar 1,2A,2B,3,4) |
| `priority_date`      | datetime | Fecha de prioridad parseada; `C`→fecha del boletín, `U`→NaN (legado). **Renombrada desde `final_action_dates`** (guardaba FAD *y* DFF → el nombre mentía) |
| `visa_bulletin_date` | datetime | Fecha del boletín mensual                             |
| `raw_value`          | str      | **Celda original** tal cual se publicó (`01MAY16`, `C`, `U`) |
| `status`             | str      | **Régimen e∈{C,F,U,UNK}** — ver abajo                 |
| `table_type`         | str      | Solo familiar: `final_action` / `dates_for_filing`    |
| `visa_wait_time`     | float    | Tiempo de espera en **años** (legado)                 |

### Columna `status` (anotación de régimen — fix H1)

Preserva el régimen que se perdía al aplanar `C`→fecha y `U`→NaN. La emite
`classify_status()` en ambos scrapers:

- `F` — se publicó una **fecha específica** (único objetivo predictivo, v5.1).
- `C` — *Current*, sin backlog ese mes (anotación descriptiva, no objetivo).
- `U` — *Unavailable*, sin números ese mes (anotación descriptiva).
- `UNK` — celda vacía o no parseable (distingue 'sin dato' de 'Unavailable'). **Centinela `UNK`, NO `NA`**: el string `"NA"` colisiona con la coerción por defecto de pandas (`read_csv` lo lee como `NaN`) y borraba la anotación; `UNK` es seguro para cualquier consumidor downstream.

### Panel consolidado `data/visa_panel_long.csv` (objetivo y_{p,c,b,t})

Generado por `build_panel.py` a partir de los 10 CSV por país. Esquema largo:

| Columna | Descripción |
|---|---|
| `country` | mexico, india, china, philippines, **all_chargeability** (= `row`) |
| `block` | `employment` / `family` |
| `category` | EB1..EB4 / F1,F2A,F2B,F3,F4 |
| `table` | `FAD` / `DFF` (ambos bloques; DFF de empleo desde Oct-2015) |
| `bulletin_date` | mes del boletín (t) |
| `status` | C/F/U/UNK |
| `priority_date` | fecha de prioridad **solo si status='F'** (NaT en C/U/UNK) |
| `days_since_base` | **variable dependiente** = días desde `BASE=1975-01-01`, **solo status='F'** |
| `raw_value` | celda cruda |

Snapshot actual: **27,127 filas · 194 series · 58% entrenable (status F)** · rango
**2001-12→2026-06** (base=1975-01-01) · `days_since_base` 0 negativos · 0 claves
duplicadas · **mega audit APTO (0 críticos, 0 advertencias)**. Cobertura a nivel
boletín = **290/295 meses (98.3%)**; los 5 ausentes (`2009-03/09/10/11`, `2012-10`)
no existen en travel.state.gov (404), solo en el archivo legacy de Wayback.

Ambos scrapers usan la **misma detección robusta** (categoría = col 0; RoW por
`'except those listed'`, resto por substring; sección por `employment[\s-]*based`
y substring `family`) y **`get_soup` con retry+backoff** (un fallo HTTP transitorio
ya NO descarta un mes en silencio; `main()` reporta cualquier mes perdido). Muchas
series EB-5 son cortas/discontinuas por cambios de régimen de categoría: cobertura
**estructural**; el filtro evaluable/piloto es posterior.

### Pendientes de cobertura (post-mega-audit, ver `mega_audit_report.md`)

- **5 meses muertos** (`2009-03/09/10/11`, `2012-10`): solo en Wayback legacy
  (`bulletin_NNNN.html`); recuperables manualmente, no auto-integrados (1.7%, el
  Action diario los borraría). Mapear el ID secuencial al mes es ambiguo.
- **Pre-2002 (→1992):** no existe en travel.state.gov (404). ⚠️ El `.tex` afirma
  "FAD desde 1992 (~408 obs)"; lo alcanzable es ~294 meses (dic-2001). **Reconciliar
  el claim del anteproyecto** o comprometerse a Wayback Machine.
- **2 hallazgos informativos** (no bugs): 6 inversiones DFF<FAD reales y 14
  retrogresiones/avances fuertes — el modelo deberá tolerarlos.

## Objetivo: VisaPredict AI

Construir modelos predictivos para forecasting de fechas del Visa Bulletin, con enfoque principal en **México**:

- **Modelos planeados:** XGBoost, modelos de series de tiempo (ARIMA, Prophet, LSTM)
- **Variable objetivo:** `priority_date` (o `days_since_base` del panel) futuras
- **Granularidad:** por país y nivel EB
- **Enfoque geográfico:** México (principal), con comparativas contra India, China, Filipinas y RoW

## Convenciones

- **Código:** en inglés (variables, funciones, comentarios técnicos)
- **Documentación académica:** en español, LaTeX/Overleaf
- **Datos crudos:** solo generados vía scripts (nunca editar CSVs manualmente)
- **Gráficos:** publication-ready para la tesis
- **Estilo:** PEP 8
- **Git:** no versionar el ambiente virtual `ante/`

## Notas para Claude Code

- Los CSVs en `data/` son **generados automáticamente** por `scrape_visa_bulletins.py`. No editarlos a mano; si necesitan cambios, modificar el scraper.
- **`visa_common.py` es la única fuente de verdad** de las funciones compartidas (`get_soup`, `extract_month_links`, `extract_datetime_from_link`, `string_to_datetime`, `classify_status`, `_norm_label`) y constantes (`SITE_ROOT`, `SCRAPER_COUNTRIES`). **NO re-duplicarlas** en los scrapers (lo estaban; deduplicadas 14-jun-2026, refactor verificado byte-idéntico). Cada scraper conserva sólo lo que difiere: `extract_tables` (detección de sección), `classify_eb_category`/`classify_family_category`, `extract_country_data`, `main`.
- El scraper tarda ~2 minutos en ejecutarse porque hace requests HTTP a cada boletín mensual individualmente.
- Los `NaN` en `visa_wait_time` corresponden a meses donde la categoría estaba `U` (Unavailable).
- El GitHub Action (`update_graphs.yml`) corre diariamente y auto-commitea si hay cambios.
- Al agregar nuevas dependencias, actualizar `requirements.txt`.
- Para análisis exploratorio o modelado, crear scripts/notebooks nuevos en la raíz o en un directorio dedicado (ej. `notebooks/`, `models/`).
- El repo original es de David Bellamy; este fork es la base de datos para la tesis de Sly.
