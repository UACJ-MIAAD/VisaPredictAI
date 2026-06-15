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

### `dim_category` — categoría migratoria
| Columna | Tipo | Notas |
|---|---|---|
| `category_id` | INTEGER PK | surrogate |
| `block` | VARCHAR | `employment` / `family` (CHECK) |
| `code` | VARCHAR | `EB1`..`EB5_*` / `F1`,`F2A`,`F2B`,`F3`,`F4` · UNIQUE(`block`,`code`) |

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
| `us_fiscal_year` | INTEGER | año fiscal federal (inicia 1-oct); los límites por país se reinician ahí |

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

## Uso

```bash
make db                      # reconstruye la BD + Parquet desde el CSV
```
```python
import duckdb
con = duckdb.connect("data/processed/visapredict.duckdb", read_only=True)
con.execute("SELECT * FROM v_panel_long WHERE country='mexico' AND category='F4'").df()
```
