# Roadmap — base de datos a nivel supremo

Plan para elevar el modelo de datos de VisaPredict AI: cobertura completa de
categorías + prácticas de modelado dimensional, gobernanza y calidad de primer
nivel. El modelo actual (esquema estrella en DuckDB, ver
[`docs/data_dictionary.md`](data_dictionary.md)) es la base; esto lo extiende.

## Punto de partida y hallazgo

El scraper captura hoy **employment (16 categorías EB) + family (5 categorías F) = 21
categorías**, todas con valor = **fecha de prioridad**. Falta una familia completa:

| Familia | ¿Capturada? | Tipo de valor |
|---|---|---|
| Family-Sponsored (F1–F4) | ✅ | fecha de prioridad |
| Employment (EB1–EB5 + subcats) | ✅ | fecha de prioridad |
| **Diversity Visa (DV) por región** | ❌ **ausente** | **número de rango** (no fecha) |
| Schedule A | ⚠️ excluida a propósito | fecha |

DV publica cortes de **rango** por región (`AFRICA`, `ASIA`, `EUROPE`,
`NORTH AMERICA (BAHAMAS)`, `OCEANIA`, `SOUTH AMERICA & CARIBBEAN`) con excepciones
de país. Como el valor es entero y no fecha, **DV no cabe** en
`priority_date`/`days_since_base` → exige generalizar el modelo.

## FASE 1 — Completitud de categorías  ◀ EN CURSO

1. **✅ Diversity Visa modelada** — `scrape_dv_visa_bulletins.py` →
   `data/raw/dv_visa_rank_timecourse.csv` (1,548 filas · 6 regiones · 258 meses,
   2004-07→2026-06) cargado en el hecho separado **`fact_dv_rank`** (grano: región ×
   mes; `rank_cutoff` INTEGER + `status`) con su **`dim_region`** y vista
   `v_dv_long`. Bajo constraints PK/FK/CHECK. Extracción robusta a 3 eras de formato.
2. **✅ `dim_region`** (6 regiones) — separada de `dim_area`.
3. **⏳ Decisión Schedule A**: pendiente (incluir como categoría o documentar exclusión).
4. **⏳ Auditoría de taxonomía** + recuperar el formato blob DV 2001-2004 y la
   *advance notification* (segunda tabla DV) — pendientes / trabajo futuro.

## FASE 2 — Modelo dimensional supremo

5. **Jerarquía de categoría** (`parent_code`, `preference_level`, base INA,
   `annual_limit`, `percountry_cap_pct`) → roll-ups (sumar todas las EB5).
6. **Bridge de alias / SCD de etiquetas** (`dim_category_alias`): etiqueta-cruda →
   código-canónico con `valid_from`/`valid_to`. Saca la normalización de 20 años de
   deriva del código a una tabla auditable y reversible.
7. **Dimensiones de referencia**: `dim_status` (significado de C/F/U/UNK).
8. **`dim_date` enriquecida**: trimestre, secuencia, marca de retrogresión.
9. **`dim_area` enriquecida**: ISO, oversubscribed, límite per-country, región.

## FASE 3 — Gobernanza, calidad y linaje

10. **Arquitectura medallón** formalizada: raw (bronze) → panel tipado (silver) →
    estrella + marts (gold).
11. **`etl_run`** (auditoría de carga: run_id, timestamp, commit, URL, filas
    cargadas/rechazadas) → provenance a nivel fila.
12. **Framework de calidad**: expectativas declarativas + score por carga + tabla
    `quarantine` para filas que violan el contrato (en vez de abortar todo).
13. **Versionado de esquema + migraciones** (`schema_version`).
14. **Capa semántica / marts**: `mart_training_F`, `mart_evaluable_series`,
    `mart_<país>_<bloque>`.

## FASE 4 — Documentación

15. Diagrama ER + catálogo + manifiesto con checksums de los outputs gold.

## Roadmap priorizado

| Fase | Entrega | Valor | Esfuerzo |
|---|---|---|---|
| **1. DV + Schedule A** | "todas las categorías" de verdad | alto | medio-alto |
| 2. Jerarquía + alias SCD | modelo rico, linaje del label-drift | alto | medio |
| 3. etl_run + calidad + marts | gobernanza + listo-para-modelar | alto | medio |
| 4. ER + catálogo | presentación/tesis | medio | bajo |

## Fuera de alcance (anti-cargo-cult, a ~27K filas)

- Partición/sharding de Parquet, tuning de índices agresivo → innecesario a esta escala.
- SCD Type-2 en `dim_area`/`dim_date` (no cambian) → solo el bridge de categoría lo amerita.
- Orquestador (Airflow/Dagster) → el cron + Makefile bastan.
- Data warehouse en la nube → descartado (reproducible y gratis; AWS solo para
  artefactos de modelado del próximo semestre vía DVC).
