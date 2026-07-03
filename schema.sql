-- VisaPredict AI — star schema for the U.S. Visa Bulletin priority-date panel.
--
-- Dimensional (star) model: one narrow fact table at the grain
-- (area x category x table x bulletin month) surrounded by four conformed
-- dimensions. The hard invariants that the pytest suite checks on the flat CSV
-- are promoted here to DECLARATIVE CONSTRAINTS (PK / UNIQUE / FK / CHECK), so the
-- schema itself rejects any row that violates the data contract on load.
--
-- The flat tidy panel y_{p,c,b,t} that the modeling stage consumes is recovered
-- losslessly by the view v_panel_long at the bottom (fact joined to its dims).

-- ─────────────────────────── DIMENSIONS ───────────────────────────

-- Country or area of chargeability. "all_chargeability" is the residual
-- administrative bucket ("All Chargeability Areas Except Those Listed"), NOT a
-- country — flagged so consumers never treat it as one.
CREATE TABLE dim_area (
    area_id            INTEGER     PRIMARY KEY,
    slug               VARCHAR     NOT NULL UNIQUE,
    name               VARCHAR     NOT NULL,
    is_residual_group  BOOLEAN     NOT NULL
);

-- Migratory category. block separates employment-based from family-sponsored;
-- code is the canonical label (EB1..EB5_*, F1/F2A/F2B/F3/F4). The hierarchy
-- columns let consumers roll a subcategory up to its parent preference:
-- parent_code (EB5_RURAL -> EB5), preference_level (the INA preference 1..5),
-- is_subcategory, and ina_basis (the statutory citation).
-- M5 ⚠️: parent_code is an INFORMATIVE label, NOT a self-join key — 'F2' (parent
-- of F2A/F2B) never appears as a row because the bulletin never publishes it, so
-- a parent_code->code self-join silently drops F2A+F2B. The canonical roll-up
-- key is (block, preference_level), which v_trainable_by_preference uses.
CREATE TABLE dim_category (
    category_id       INTEGER  PRIMARY KEY,
    block             VARCHAR  NOT NULL CHECK (block IN ('employment', 'family')),
    code              VARCHAR  NOT NULL,
    parent_code       VARCHAR,
    preference_level  INTEGER  NOT NULL CHECK (preference_level BETWEEN 1 AND 5),
    is_subcategory    BOOLEAN  NOT NULL,
    ina_basis         VARCHAR,
    UNIQUE (block, code)
);

-- Bulletin table type: Final Action Dates vs Dates for Filing (evaluated
-- separately, never compared directly).
CREATE TABLE dim_table (
    table_id  INTEGER  PRIMARY KEY,
    code      VARCHAR  NOT NULL UNIQUE CHECK (code IN ('FAD', 'DFF')),
    name      VARCHAR  NOT NULL
);

-- Bulletin month (time dimension). us_fiscal_year follows the U.S. federal year
-- (starts Oct 1), useful because per-country limits reset on the fiscal boundary.
CREATE TABLE dim_date (
    date_id         INTEGER  PRIMARY KEY,
    bulletin_date   DATE     NOT NULL UNIQUE,
    year            INTEGER  NOT NULL,
    month           INTEGER  NOT NULL CHECK (month BETWEEN 1 AND 12),
    quarter         INTEGER  NOT NULL CHECK (quarter BETWEEN 1 AND 4),
    us_fiscal_year  INTEGER  NOT NULL
);

-- Administrative regime of a published cell, promoted from a CHECK to a
-- conformed dimension so its meaning is documented and joinable. is_predictable
-- marks the only regime that is a modeling target ('F').
CREATE TABLE dim_status (
    status          VARCHAR  PRIMARY KEY CHECK (status IN ('C', 'F', 'U', 'UNK')),
    label           VARCHAR  NOT NULL,
    description     VARCHAR  NOT NULL,
    is_predictable  BOOLEAN  NOT NULL
);

-- ─────────────────────────── FACT ───────────────────────────

-- One row per (area, category, table, month). days_since_base is the dependent
-- variable y_{p,c,b,t}; it and priority_date exist ONLY for status 'F' (a
-- published specific date) — the v5.1 formulation. C/U/UNK are kept as
-- descriptive annotation, never as a prediction target.
CREATE TABLE fact_priority (
    area_id          INTEGER  NOT NULL REFERENCES dim_area(area_id),
    category_id      INTEGER  NOT NULL REFERENCES dim_category(category_id),
    table_id         INTEGER  NOT NULL REFERENCES dim_table(table_id),
    date_id          INTEGER  NOT NULL REFERENCES dim_date(date_id),
    status           VARCHAR  NOT NULL REFERENCES dim_status(status) CHECK (status IN ('C', 'F', 'U', 'UNK')),
    priority_date    DATE,
    days_since_base  INTEGER  CHECK (days_since_base IS NULL OR days_since_base >= 0),
    raw_value        VARCHAR,
    PRIMARY KEY (area_id, category_id, table_id, date_id),
    -- The dependent variable and the priority date are defined IFF status='F'.
    -- Named so a violation reports the exact invariant, not just the table.
    CONSTRAINT days_iff_F  CHECK ((status = 'F') = (days_since_base IS NOT NULL)),
    CONSTRAINT pdate_iff_F CHECK ((status = 'F') = (priority_date  IS NOT NULL)),
    -- M5: the arithmetic contract of the dependent variable (t0 = 1975-01-01)
    -- used to live only in build_panel.py; a BASE_EPOCH change without a rebuild
    -- would shift the whole target with every gate green.
    CONSTRAINT days_is_datediff CHECK (
        days_since_base IS NULL OR days_since_base = datediff('day', DATE '1975-01-01', priority_date)
    )
);

-- ─────────────────────────── PANEL VIEW ───────────────────────────

-- Lossless reconstruction of the tidy long panel the ML stage trains on.
-- Column names and order match data/processed/visa_panel_long.csv exactly.
CREATE VIEW v_panel_long AS
SELECT
    a.slug             AS country,
    c.block            AS block,
    c.code             AS category,
    t.code             AS "table",
    d.bulletin_date    AS bulletin_date,
    f.status           AS status,
    f.priority_date    AS priority_date,
    f.days_since_base  AS days_since_base,
    f.raw_value        AS raw_value
FROM fact_priority f
JOIN dim_area     a ON a.area_id     = f.area_id
JOIN dim_category c ON c.category_id = f.category_id
JOIN dim_table    t ON t.table_id    = f.table_id
JOIN dim_date     d ON d.date_id     = f.date_id;

-- Roll-up enabled by the category hierarchy: trainable ('F') observations folded
-- to block x preference level (so EB5_RURAL, EB5_RC, … all count under EB-5).
CREATE VIEW v_trainable_by_preference AS
SELECT c.block AS block, c.preference_level AS preference_level, count(*) AS n_obs
FROM fact_priority f
JOIN dim_category c ON c.category_id = f.category_id
WHERE f.status = 'F'
GROUP BY c.block, c.preference_level;

-- ─────────────────────────── CATEGORY ALIAS BRIDGE ───────────────────────────

-- Lineage of 20 years of label drift, lifted OUT of the classify_*() code into
-- data: every raw category label as the bulletin actually published it, mapped
-- to its canonical category, with the window of months it was observed. Makes
-- the normalization auditable ("which spellings became EB5_RC, and when?").
CREATE TABLE dim_category_alias (
    alias_id     INTEGER  PRIMARY KEY,
    category_id  INTEGER  NOT NULL REFERENCES dim_category(category_id),
    raw_label    VARCHAR  NOT NULL,
    valid_from   DATE     NOT NULL,
    valid_to     DATE     NOT NULL,
    n_months     INTEGER  NOT NULL CHECK (n_months > 0),
    UNIQUE (category_id, raw_label),
    CHECK (valid_from <= valid_to),
    -- P1: n_months counts DISTINCT observed months, which can never exceed the
    -- envelope span. (valid_from/valid_to are min/max envelopes, NOT SCD-2
    -- validity ranges — windows of the same canonical overlap legitimately.)
    CHECK (n_months <= datediff('month', valid_from, valid_to) + 1)
);

CREATE VIEW v_category_alias AS
SELECT c.block AS block, c.code AS canonical, x.raw_label, x.valid_from, x.valid_to, x.n_months
FROM dim_category_alias x
JOIN dim_category c ON c.category_id = x.category_id;

-- ─────────────────────────── DIVERSITY VISA (DV) ───────────────────────────

-- DV is published as a regional RANK NUMBER, not a priority date, so it gets its
-- own dimension + fact instead of polluting the date panel. There is no
-- Final-Action/Dates-for-Filing split for DV (the second chart a bulletin prints
-- is an advance notification of a future month — out of scope for now), so the
-- grain here is simply region x bulletin month.

CREATE TABLE dim_region (
    region_id  INTEGER  PRIMARY KEY,
    slug       VARCHAR  NOT NULL UNIQUE,
    name       VARCHAR  NOT NULL
);

CREATE TABLE fact_dv_rank (
    region_id    INTEGER  NOT NULL REFERENCES dim_region(region_id),
    date_id      INTEGER  NOT NULL REFERENCES dim_date(date_id),
    status       VARCHAR  NOT NULL REFERENCES dim_status(status) CHECK (status IN ('C', 'F', 'U', 'UNK')),
    rank_cutoff  INTEGER  CHECK (rank_cutoff IS NULL OR rank_cutoff >= 0),
    raw_value    VARCHAR,
    exceptions   VARCHAR,
    PRIMARY KEY (region_id, date_id),
    -- The rank cut-off is defined IFF a specific number is published (status 'F').
    -- M5: named like days_iff_F/pdate_iff_F so a violation reports the invariant.
    CONSTRAINT rank_iff_F CHECK ((status = 'F') = (rank_cutoff IS NOT NULL))
);

CREATE VIEW v_dv_long AS
SELECT
    r.slug          AS region,
    d.bulletin_date AS bulletin_date,
    f.status        AS status,
    f.rank_cutoff   AS rank_cutoff,
    f.raw_value     AS raw_value,
    f.exceptions    AS exceptions
FROM fact_dv_rank f
JOIN dim_region r ON r.region_id = f.region_id
JOIN dim_date   d ON d.date_id   = f.date_id;

-- ─────────────────────────── GOVERNANCE & PROVENANCE ───────────────────────

-- Structural schema version; bump on any change so consumers/migrations detect
-- drift. M5: PRIMARY KEY caps accumulation — fetchone() consumers read "the"
-- version, so N silently accumulated rows would lie.
CREATE TABLE schema_version (
    version      INTEGER  PRIMARY KEY,
    description  VARCHAR  NOT NULL
);

-- Load audit / provenance + per-build data-quality summary. The database is
-- rebuilt wholesale, so provenance is at the BUILD level (one row per build),
-- not per fact row — row-level run_id would be uniform and add no signal.
CREATE TABLE etl_run (
    run_id           INTEGER    PRIMARY KEY,
    built_at_utc     TIMESTAMP  NOT NULL,
    schema_version   INTEGER    NOT NULL,
    n_fact_priority  INTEGER    NOT NULL,
    n_fact_dv        INTEGER    NOT NULL,
    n_trainable_f    INTEGER    NOT NULL,
    pct_trainable    DOUBLE     NOT NULL CHECK (pct_trainable BETWEEN 0 AND 1),
    panel_floor      DATE       NOT NULL,
    panel_ceiling    DATE       NOT NULL
);

-- ─────────────────────────── GOLD MARTS (modeling) ───────────────────────────

-- The clean training set: only trainable ('F') observations with the dependent
-- variable and useful time features. This is what the modeling stage consumes.
CREATE VIEW mart_training_F AS
SELECT
    a.slug AS country, c.block AS block, c.code AS category, t.code AS "table",
    d.bulletin_date AS bulletin_date, d.year AS year, d.month AS month, d.quarter AS quarter,
    c.preference_level AS preference_level,
    f.priority_date AS priority_date, f.days_since_base AS days_since_base
FROM fact_priority f
JOIN dim_area     a ON a.area_id     = f.area_id
JOIN dim_category c ON c.category_id = f.category_id
JOIN dim_table    t ON t.table_id    = f.table_id
JOIN dim_date     d ON d.date_id     = f.date_id
WHERE f.status = 'F';

-- Per-series summary so the modeling stage can select "evaluable" series
-- (enough trainable points, long enough span, some variability).
CREATE VIEW mart_series_summary AS
SELECT
    a.slug AS country, c.block AS block, c.code AS category, t.code AS "table",
    count(*) AS n_obs,
    count(*) FILTER (WHERE f.status = 'F') AS n_trainable,
    min(d.bulletin_date) AS first_month,
    max(d.bulletin_date) AS last_month,
    count(DISTINCT f.status) AS n_regimes
FROM fact_priority f
JOIN dim_area     a ON a.area_id     = f.area_id
JOIN dim_category c ON c.category_id = f.category_id
JOIN dim_table    t ON t.table_id    = f.table_id
JOIN dim_date     d ON d.date_id     = f.date_id
GROUP BY a.slug, c.block, c.code, t.code;
