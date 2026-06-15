# Diagrama entidad-relación — VisaPredict AI

Modelo dimensional (esquema estrella) del almacén de datos. Diagrama de arquitectura
en [`schema_er.svg`](schema_er.svg); el ER completo (todas las columnas, PK/FK,
cardinalidad) se renderiza nativo en GitHub abajo. Definición autoritativa:
[`schema.sql`](../schema.sql) · catálogo: [`data_dictionary.md`](data_dictionary.md).

![Esquema estrella VisaPredict AI](schema_er.svg)

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

    fact_priority {
        int     area_id          PK_FK
        int     category_id      PK_FK
        int     table_id         PK_FK
        int     date_id          PK_FK
        varchar status           FK
        date    priority_date    "iff F"
        int     days_since_base  "y · iff F"
        varchar raw_value        "lineage"
    }
    fact_dv_rank {
        int     region_id    PK_FK
        int     date_id      PK_FK
        varchar status       FK
        int     rank_cutoff  "iff F"
        varchar raw_value    "lineage"
        varchar exceptions   "per-country"
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
        int     alias_id     PK
        int     category_id  FK
        varchar raw_label    "as published"
        date    valid_from
        date    valid_to
        int     n_months
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
        int       run_id           PK
        timestamp built_at_utc
        int       schema_version
        int       n_fact_priority
        double    pct_trainable    "quality"
        date      panel_floor
        date      panel_ceiling
    }
    schema_version {
        int     version
        varchar description
    }
```

## Capas medallón

```
data/raw/*.csv  ─bronze→  visa_panel_long.csv  ─silver→  estrella + marts (gold)
 (fuente cruda)            (panel tipado)                 DuckDB · mart_training_F …
```

**Dimensiones conformes** (compartidas por ambos hechos): `dim_date`, `dim_status`.
**Marts gold** (vistas para el modelado): `mart_training_F`, `mart_series_summary`.
