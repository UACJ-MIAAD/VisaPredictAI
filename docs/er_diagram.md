# Diagrama entidad-relación — VisaPredict AI

Modelo dimensional (esquema estrella) del almacén de datos. Diagrama de arquitectura
en [`schema_er.svg`](schema_er.svg); el ER completo (todas las columnas, PK/FK,
cardinalidad) se renderiza nativo en GitHub abajo. Definición autoritativa:
[`schema.sql`](../schema.sql) · catálogo: [`data_dictionary.md`](data_dictionary.md).

![Esquema estrella VisaPredict AI](schema_er.svg)

> Nota: `schema_er.svg` muestra la arquitectura de alto nivel previa a la
> migración 002 (aún sin `source_artifact` ni las columnas de procedencia); el
> ER completo de abajo sí refleja el esquema vigente.

## ER completo

```mermaid
erDiagram
    dim_area     ||--o{ fact_priority   : "area_id"
    dim_category ||--o{ fact_priority   : "category_id"
    dim_table    ||--o{ fact_priority   : "table_id"
    dim_date     ||--o{ fact_priority   : "date_id"
    dim_status   ||--o{ fact_priority   : "status"
    dim_region   ||--o{ fact_dv_rank    : "region_id"
    dim_date     ||--o{ fact_dv_rank    : "date_id"
    dim_status   ||--o{ fact_dv_rank    : "status"
    dim_category ||--o{ dim_category_alias : "category_id"
    schema_version ||--o{ etl_run       : "schema_version"
    etl_run      ||--o{ fact_priority   : "etl_run_id (asserted)"
    etl_run      ||--o{ fact_dv_rank    : "etl_run_id (asserted)"
    dim_date     ||--o{ source_artifact : "vintage (by value)"

    fact_priority {
        int         area_id          PK_FK
        int         category_id      PK_FK
        int         table_id         PK_FK
        int         date_id          PK_FK
        varchar     status           FK
        date        priority_date    "iff F"
        int         days_since_base  "y · iff F"
        varchar     raw_value        "lineage"
        int         etl_run_id       "run linkage (H2)"
        timestamptz created_at       "bulletin month (H4)"
        timestamptz updated_at       "data vintage on change"
    }
    fact_dv_rank {
        int         region_id    PK_FK
        int         date_id      PK_FK
        varchar     status       FK
        int         rank_cutoff  "iff F"
        varchar     raw_value    "lineage"
        varchar     exceptions   "per-country"
        int         etl_run_id   "run linkage (H2)"
        timestamptz created_at   "bulletin month (H4)"
        timestamptz updated_at   "data vintage on change"
    }
    source_artifact {
        int         source_id           PK
        varchar     filename            UK
        varchar     url                 "S3 archival URI"
        varchar     license             "US Gov public domain"
        varchar     sha256              "frozen HTML hash"
        date        vintage             "bulletin month"
        timestamptz source_modified_at  "NULL (not tracked)"
        timestamptz created_at
        timestamptz updated_at
    }
    dim_area {
        int     area_id            PK
        varchar slug               UK
        varchar name
        boolean is_residual_group
    }
    dim_category {
        int     category_id       PK
        varchar block
        varchar code
        varchar parent_code       "roll-up"
        int     preference_level
        boolean is_subcategory
        varchar ina_basis
    }
    dim_category_alias {
        int         alias_id     PK
        int         category_id  FK
        varchar     raw_label    "as published"
        date        valid_from
        date        valid_to
        int         n_months
        timestamptz created_at   "= valid_from"
        timestamptz updated_at   "= valid_to"
    }
    dim_status {
        varchar status          PK
        varchar label
        varchar description
        boolean is_predictable  "F only"
    }
    dim_table {
        int     table_id  PK
        varchar code      UK
        varchar name
    }
    dim_date {
        int     date_id         PK
        date    bulletin_date   UK
        int     year
        int     month
        int     quarter
        int     us_fiscal_year
    }
    dim_region {
        int     region_id  PK
        varchar slug       UK
        varchar name
    }
    etl_run {
        int         run_id           PK
        timestamptz built_at_utc
        int         schema_version   FK
        int         n_fact_priority
        double      pct_trainable    "quality"
        date        panel_floor
        date        panel_ceiling
        varchar     pipeline_run_id  "CLI/env"
        varchar     git_sha          "full 40 chars"
        boolean     git_dirty
        varchar     panel_sha256
        varchar     dvc_lock_sha256
        varchar     env_lock_sha256
        timestamptz started_at
        timestamptz completed_at
        varchar     build_status     "ok | degraded"
        varchar     degradations     "reasons"
    }
    schema_version {
        int         version      PK
        varchar     description
        timestamptz applied_at
        varchar     checksum     "sha256 of migration file"
    }
```

## Capas medallón

```
data/raw/*.csv  ─bronze→  visa_panel_long.csv  ─silver→  estrella + marts (gold)
 (fuente cruda)            (panel tipado)                 DuckDB · mart_training_F …
```

**Dimensiones conformes** (compartidas por ambos hechos): `dim_date`, `dim_status`.
**Marts gold** (vistas para el modelado): `mart_training_F`, `mart_series_summary`.
