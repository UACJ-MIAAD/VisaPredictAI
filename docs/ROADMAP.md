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

## FASE 1 — Completitud de categorías  ✅ COMPLETA

1. **✅ Diversity Visa modelada** — `scrape_dv_visa_bulletins.py` →
   `data/raw/dv_visa_rank_timecourse.csv` (**1,605 filas · 6 regiones · 268 meses,
   2001-12→2026-06**, el mismo piso que el panel) cargado en el hecho separado
   **`fact_dv_rank`** + **`dim_region`** + vista `v_dv_long`, bajo PK/FK/CHECK.
2. **✅ `dim_region`** (6 regiones) — separada de `dim_area`.
3. **✅ Decisión Schedule A**: **excluida con evidencia** — no es una categoría con
   fecha propia (no aparece como fila con corte en 2002/2007/2020; es certificación
   laboral dentro de EB-3). Documentado en `docs/data_dictionary.md`.
4. **✅ Cobertura completa + auditoría de taxonomía**: el **formato blob 2001-2004
   recuperado** (`extract_dv_blob`) lleva DV al piso del proyecto; `test_category_taxonomy_complete`
   fija la taxonomía (21 cats + 6 regiones). **Fuera de alcance documentado**: la
   *advance notification* (2ª tabla DV = mes futuro, otra serie).

## FASE 2 — Modelo dimensional supremo  ◀ EN CURSO

5. **✅ Jerarquía de categoría** — `dim_category` + `parent_code`,
   `preference_level`, `is_subcategory`, `ina_basis`; vista `v_trainable_by_preference`
   hace roll-ups (todas las `EB5_*` bajo EB-5). (`annual_limit`/`percountry_cap`
   omitidos: cifras estatutarias con matices — riesgo de error > valor.)
6. **⏳ Bridge de alias / SCD de etiquetas** (`dim_category_alias`): etiqueta-cruda →
   código-canónico con `valid_from`/`valid_to`. **Pendiente** — requiere capturar la
   etiqueta cruda en los scrapers (hoy el CSV ya guarda el código canónico).
7. **✅ `dim_status`** — dimensión conformada (C/F/U/UNK + label + descripción +
   `is_predictable`), con **FK desde ambos hechos**.
8. **✅ `dim_date` enriquecida** — `quarter` (estacionalidad). (`bulletin_seq` = `date_id`,
   redundante; marca de retrogresión = derivable en consulta.)
9. **⏳ `dim_area` enriquecida**: pendiente — el valor marginal es bajo (ya tiene
   `is_residual_group`); se evaluará si el modelado lo pide.

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
