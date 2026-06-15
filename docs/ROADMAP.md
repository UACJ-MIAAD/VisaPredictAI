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
4. **✅ Cobertura + auditoría de taxonomía**: el **formato blob recuperado
   PARCIALMENTE** (`extract_dv_blob`) lleva el piso DV a 2001-12, pero 2002-2003 es
   parcial (~20 meses en HTML no-tabular, pendientes). `test_category_taxonomy_complete`
   fija la taxonomía (21 cats + 6 regiones) y **`test_dv_coverage_floor`** (gate del
   cron) protege la cobertura DV contra degradación silenciosa. **Fuera de alcance
   documentado**: la *advance notification* (2ª tabla DV = mes futuro) y el HTML
   no-tabular 2002-2003.

## FASE 2 — Modelo dimensional supremo  ✅ COMPLETA

5. **✅ Jerarquía de categoría** — `dim_category` + `parent_code`,
   `preference_level`, `is_subcategory`, `ina_basis`; vista `v_trainable_by_preference`
   hace roll-ups (todas las `EB5_*` bajo EB-5). (`annual_limit`/`percountry_cap`
   omitidos: cifras estatutarias con matices — riesgo de error > valor.)
6. **✅ Bridge de alias / linaje de etiquetas** (`dim_category_alias`): los scrapers
   ahora preservan la etiqueta cruda (`raw_category`); el bridge mapea cada grafía
   publicada → canónico con `valid_from`/`valid_to`/`n_months`. **48 alias** sobre 21
   categorías (EB5_TEA: 7 grafías 2001-2015). Vista `v_category_alias`.
7. **✅ `dim_status`** — dimensión conformada (C/F/U/UNK + label + descripción +
   `is_predictable`), con **FK desde ambos hechos**.
8. **✅ `dim_date` enriquecida** — `quarter` (estacionalidad). (`bulletin_seq` = `date_id`,
   redundante; marca de retrogresión = derivable en consulta.)
9. **➖ `dim_area` enriquecida**: omitida — valor marginal bajo (ya tiene
   `is_residual_group`); se reevaluará si el modelado lo pide.

## FASE 3 — Gobernanza, calidad y linaje  ✅ COMPLETA

10. **✅ Arquitectura medallón** documentada: raw (bronze) → panel tipado (silver) →
    estrella + marts (gold).
11. **✅ `etl_run`** — auditoría de carga a nivel build (`built_at_utc`,
    `schema_version`, conteos, `pct_trainable`, floor/ceiling). Row-level run_id
    omitido (la BD se reconstruye entera → sería uniforme, sin señal).
12. **✅ Calidad** vía score en `etl_run` + rechazo en el esquema (PK/FK/CHECK) +
    gate de CI. **Sin `quarantine`** a conciencia: el dato entra limpio del gate de
    `build_panel`, así que la tabla estaría vacía.
13. **✅ `schema_version`**. Migraciones in-situ omitidas: la BD es **regenerable**
    (`make db`), no se migra en el lugar.
14. **✅ Marts gold**: `mart_training_F` (set de entrenamiento limpio) y
    `mart_series_summary` (resumen por serie para filtrar evaluables).

## FASE 4 — Documentación  ✅ COMPLETA

15. **✅ Diagrama ER + catálogo.** ER en [`docs/er_diagram.md`](er_diagram.md):
    Mermaid renderizado nativo en GitHub (todas las columnas + cardinalidad) +
    **hero SVG** [`docs/schema_er.svg`](schema_er.svg) (estrella, paleta UACJ,
    dimensiones conformes resaltadas, medallón). Catálogo = [`data_dictionary.md`](data_dictionary.md).
    (Manifiesto con checksums omitido: los outputs gold son regenerables/gitignored;
    el CSV versionado y `etl_run` ya dan trazabilidad.)

---

**Roadmap completo: Fases 1–4 ✅.** La base de datos quedó de nivel supremo:
todas las categorías (incl. Diversity Visa), modelo dimensional con jerarquía +
linaje + dimensiones conformes, gobernanza + marts de modelado, y documentación
con ER + catálogo.

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
