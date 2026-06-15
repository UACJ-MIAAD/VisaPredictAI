# Diccionario de datos — VisaPredict AI

Modelo dimensional (esquema estrella) del panel de fechas de prioridad del
*U.S. Visa Bulletin*. La definición autoritativa de tablas, tipos y constraints
vive en [`schema.sql`](../schema.sql); este documento la describe en prosa.

## Capas

| Artefacto | Formato | Versionado | Cómo se genera |
|---|---|---|---|
| `data/raw/*.csv` | CSV | sí (git) | scrapers (`scrape_*_visa_bulletins.py`) — fuente inmutable |
| `data/processed/visa_panel_long.csv` | CSV largo | **sí (git)** — entregable abierto | `build_panel.py` |
| `data/processed/visapredict.duckdb` | DuckDB (estrella) | no (regenerable) | `build_database.py` / `make db` |
| `data/processed/visa_panel_long.parquet` | Parquet tipado | no (regenerable) | `build_database.py` / `make db` |

El CSV plano es la **fuente de verdad abierta**; la base DuckDB y el Parquet se
**reconstruyen** desde él con `make db` (por eso están gitignored: cero *bloat*
binario, cero deriva respecto al CSV).

## Grano

El hecho registra **una observación por** `(área × categoría × tabla × mes de
boletín)`. La variable dependiente `days_since_base` ($y_{p,c,b,t}$) existe
**solo** cuando `status = 'F'` (se publicó una fecha específica); en `C`/`U`/`UNK`
es nula y la celda se conserva como anotación descriptiva (formulación v5.1).

## Dimensiones

### `dim_area` — país o área de cargabilidad
| Columna | Tipo | Notas |
|---|---|---|
| `area_id` | INTEGER PK | clave surrogate |
| `slug` | VARCHAR UNIQUE | `mexico`, `india`, `china`, `philippines`, `all_chargeability` |
| `name` | VARCHAR | nombre legible |
| `is_residual_group` | BOOLEAN | `true` solo para *All Chargeability Areas Except Those Listed* (bucket residual, **no** un país) |

### `dim_category` — categoría migratoria (con jerarquía)
| Columna | Tipo | Notas |
|---|---|---|
| `category_id` | INTEGER PK | surrogate |
| `block` | VARCHAR | `employment` / `family` (CHECK) |
| `code` | VARCHAR | `EB1`..`EB5_*` / `F1`,`F2A`,`F2B`,`F3`,`F4` · UNIQUE(`block`,`code`) |
| `parent_code` | VARCHAR | preferencia padre para roll-up (`EB5_RURAL`→`EB5`, `F2A`→`F2`); NULL si es top-level |
| `preference_level` | INTEGER | preferencia INA 1–5 (CHECK) |
| `is_subcategory` | BOOLEAN | true para `EB3_OW`, `EB4_RW/TRANS`, `EB5_*`, `F2A/F2B` |
| `ina_basis` | VARCHAR | cita estatutaria (`INA 203(b)(5)`, `INA 203(a)(2)(A)`…) |

### `dim_status` — régimen administrativo (dimensión conformada)
| Columna | Tipo | Notas |
|---|---|---|
| `status` | VARCHAR PK | `C`/`F`/`U`/`UNK` (CHECK); **FK desde ambos hechos** |
| `label` | VARCHAR | Current / Final / Unavailable / Unknown |
| `description` | VARCHAR | significado |
| `is_predictable` | BOOLEAN | true **solo** para `F` (único objetivo de modelado) |

### `dim_table` — tipo de tabla
| Columna | Tipo | Notas |
|---|---|---|
| `table_id` | INTEGER PK | surrogate |
| `code` | VARCHAR UNIQUE | `FAD` / `DFF` (CHECK) |
| `name` | VARCHAR | *Final Action Dates* / *Dates for Filing* |

### `dim_date` — mes del boletín
| Columna | Tipo | Notas |
|---|---|---|
| `date_id` | INTEGER PK | surrogate |
| `bulletin_date` | DATE UNIQUE | primer día del mes del boletín |
| `year` / `month` | INTEGER | `month` 1–12 (CHECK) |
| `quarter` | INTEGER | trimestre 1–4 (CHECK) — útil para estacionalidad |
| `us_fiscal_year` | INTEGER | año fiscal federal (inicia 1-oct); los límites por país se reinician ahí |

**Roll-up:** la vista `v_trainable_by_preference` agrega las observaciones
entrenables (`status='F'`) por `block × preference_level`, plegando las
subcategorías a su preferencia (todas las `EB5_*` cuentan bajo EB-5).

### `dim_category_alias` — bridge de linaje (deriva de etiquetas)

Saca a **datos auditables** los 20 años de deriva de etiquetas que antes vivían
enterrados en `classify_*()`: cada etiqueta cruda tal como el boletín la publicó,
mapeada a su categoría canónica, con la ventana de meses en que se observó. Se
construye desde la columna `raw_category` de los CSV crudos. **48 alias** sobre 21
categorías (p. ej. `EB5_TEA` tuvo 7 grafías distintas 2001-2015).

| Columna | Tipo | Notas |
|---|---|---|
| `alias_id` | INTEGER PK | surrogate |
| `category_id` | INTEGER FK | → `dim_category` |
| `raw_label` | VARCHAR | etiqueta publicada (whitespace colapsado) · UNIQUE(`category_id`,`raw_label`) |
| `valid_from` / `valid_to` | DATE | primer/último mes observado (CHECK `from ≤ to`) |
| `n_months` | INTEGER | meses en que apareció (CHECK > 0) |

Vista `v_category_alias` une el bridge con `dim_category` (expone `block` +
`canonical`). Responde "¿qué grafías se volvieron `EB5_RC`, y cuándo?".

## Hecho: `fact_priority`
| Columna | Tipo | Notas |
|---|---|---|
| `area_id`,`category_id`,`table_id`,`date_id` | INTEGER FK | **PK compuesta** → unicidad de la serie |
| `status` | VARCHAR | `C`/`F`/`U`/`UNK` (CHECK) |
| `priority_date` | DATE | fecha de prioridad publicada; **no nula solo si `F`** |
| `days_since_base` | INTEGER | días desde `1975-01-01`; **no nulo solo si `F`**; ≥ 0 (CHECK) |
| `raw_value` | VARCHAR | celda original tal cual se publicó (`01MAY16`, `C`, `U`) — linaje |

**Constraints declarativas (el esquema *es* el contrato):**
- PK compuesta + FK a las 4 dimensiones (integridad referencial).
- `CHECK status IN ('C','F','U','UNK')`.
- `CHECK days_since_base IS NULL OR >= 0`.
- `CHECK (status='F') = (days_since_base IS NOT NULL)` y lo mismo para `priority_date`.
- `priority_date ≤ bulletin_date` se valida en `tests/test_database.py` (es cruce de tablas, no constraint de columna).

## Vista: `v_panel_long`

Reconstruye **sin pérdida** el panel tidy `y_{p,c,b,t}` (mismas columnas y orden
que el CSV) uniendo el hecho con sus dimensiones. Es lo que consume el modelado.

## Diversity Visa (DV)

DV se publica como un **número de rango** regional, no una fecha, así que vive en
su propio hecho `fact_dv_rank` (no contamina el panel de fechas). Fuente:
`data/raw/dv_visa_rank_timecourse.csv` (**1,605 filas · 6 regiones · 268 meses,
2001-12→2026-06** — el mismo piso temporal que el panel). El parser maneja **dos
formatos**: el tabular moderno y, como fallback, el **blob de una sola celda**
2001-2004 (`AFRICA: AF 21,400 …`, ver `extract_dv_blob`). Fuera de alcance: la
*advance notification* (la 2ª tabla DV es un mes futuro, no FAD/DFF — sería otra
serie con mes-objetivo distinto).

> **Schedule A.** No se modela porque **no es una categoría con fecha propia**: no
> aparece como fila con corte en ningún boletín (verificado 2002/2007/2020); es un
> mecanismo de certificación laboral contabilizado dentro de EB-3. `classify_eb_category`
> solo descarta el header "Employment-Based", no filas Schedule A.

### `dim_region`
| Columna | Tipo | Notas |
|---|---|---|
| `region_id` | INTEGER PK | surrogate |
| `slug` | VARCHAR UNIQUE | `africa`,`asia`,`europe`,`north_america`,`oceania`,`south_america_caribbean` |
| `name` | VARCHAR | nombre legible |

### `fact_dv_rank` (grano: región × mes)
| Columna | Tipo | Notas |
|---|---|---|
| `region_id`,`date_id` | INTEGER FK | **PK compuesta** |
| `status` | VARCHAR | `C`/`F`/`U`/`UNK` (CHECK) |
| `rank_cutoff` | INTEGER | número de rango; **no nulo solo si `F`** (CHECK), ≥ 0 |
| `raw_value` | VARCHAR | celda original (`55,000`, `CURRENT`) |
| `exceptions` | VARCHAR | cortes por país (`Except: Egypt 30,000`) |

Vista `v_dv_long` = `fact_dv_rank ⨝ dim_region ⨝ dim_date`.

## Uso

```bash
make db                      # reconstruye la BD + Parquet desde el CSV
```
```python
import duckdb
con = duckdb.connect("data/processed/visapredict.duckdb", read_only=True)
con.execute("SELECT * FROM v_panel_long WHERE country='mexico' AND category='F4'").df()
```
