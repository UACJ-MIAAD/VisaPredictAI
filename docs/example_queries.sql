-- ============================================================================
-- VisaPredict AI — starter queries for the DuckDB star schema
-- ============================================================================
-- DB file: data/processed/visapredict.duckdb  (regenerate with `make db`)
--
-- Run the whole file from the CLI:
--     duckdb data/processed/visapredict.duckdb < docs/example_queries.sql
-- Or in DBeaver: open this file, put the cursor on a statement and press
-- Ctrl+Enter to run just that one.
--
-- Star schema: fact_priority (grain area x category x table x month) + 5
-- conformed dimensions; DV lives in its own fact (fact_dv_rank). The flat tidy
-- panel the modeling stage trains on is rebuilt losslessly by v_panel_long.
-- ============================================================================


-- ── 0. Orientation ─────────────────────────────────────────────────────────
-- Every table and view in the database.
SELECT table_name, table_type
FROM information_schema.tables
ORDER BY table_type, table_name;

-- Row counts of the two fact tables.
SELECT 'fact_priority' AS fact, count(*) AS rows FROM fact_priority
UNION ALL
SELECT 'fact_dv_rank', count(*) FROM fact_dv_rank;


-- ── 1. The tidy panel (lossless rebuild of visa_panel_long.csv) ─────────────
-- Full long panel y_{p,c,b,t}.
SELECT * FROM v_panel_long
ORDER BY country, block, category, "table", bulletin_date
LIMIT 50;

-- One series, newest first.
SELECT bulletin_date, status, priority_date, days_since_base, raw_value
FROM v_panel_long
WHERE country = 'mexico' AND category = 'F3' AND "table" = 'FAD'
ORDER BY bulletin_date DESC
LIMIT 24;

-- Status mix across the whole panel (only 'F' is a prediction target).
SELECT status, count(*) AS rows,
       round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct
FROM v_panel_long
GROUP BY status
ORDER BY rows DESC;


-- ── 2. The trainable set (what the modeling stage consumes) ─────────────────
-- mart_training_F = only status 'F', with the dependent variable + time features.
SELECT * FROM mart_training_F
WHERE country = 'mexico' AND category = 'EB2'
ORDER BY "table", bulletin_date DESC
LIMIT 30;


-- ── 3. Per-series summary (use this to pick the "evaluable" series) ─────────
-- n_trainable = how many 'F' points; n_regimes = how many of C/F/U/UNK appear.
SELECT country, category, "table", n_obs, n_trainable, n_regimes,
       first_month, last_month
FROM mart_series_summary
ORDER BY n_trainable DESC
LIMIT 25;

-- Series that are probably too short/sparse to model (a modeling-stage filter).
SELECT country, category, "table", n_obs, n_trainable, first_month, last_month
FROM mart_series_summary
WHERE n_trainable < 24
ORDER BY n_trainable ASC;


-- ── 4. Diversity Visa (separate fact: regional rank, NOT a date target) ─────
SELECT region, bulletin_date, status, rank_cutoff, exceptions
FROM v_dv_long
WHERE region = 'africa'
ORDER BY bulletin_date DESC
LIMIT 24;


-- ── 5. Label lineage (20 years of drift, lifted out of code into data) ──────
-- Which raw published labels became each canonical category, and when.
SELECT block, canonical, raw_label, valid_from, valid_to, n_months
FROM v_category_alias
WHERE canonical LIKE 'EB5%'
ORDER BY canonical, valid_from;


-- ── 6. Category hierarchy / roll-up ─────────────────────────────────────────
-- Trainable observations folded to block x preference level (EB5_* all under 5).
SELECT block, preference_level, n_obs
FROM v_trainable_by_preference
ORDER BY block, preference_level;

-- The category dimension with its statutory metadata.
SELECT block, code, parent_code, preference_level, is_subcategory, ina_basis
FROM dim_category
ORDER BY block, preference_level, code;


-- ── 7. Governance / provenance (one row per build) ──────────────────────────
SELECT * FROM etl_run;
SELECT * FROM schema_version;


-- ── 8. A few analytical examples ────────────────────────────────────────────
-- Latest published Final Action Date per country for the F-family categories.
SELECT country, category, max(bulletin_date) AS latest_month,
       arg_max(priority_date, bulletin_date) AS latest_priority_date
FROM mart_training_F
WHERE block = 'family' AND "table" = 'FAD'
GROUP BY country, category
ORDER BY country, category;

-- Month-over-month retrogressions (priority date moved BACKWARD) in Mexico F-FAD.
SELECT country, category, bulletin_date, priority_date,
       priority_date - lag(priority_date) OVER (
           PARTITION BY country, category, "table" ORDER BY bulletin_date
       ) AS delta_days
FROM mart_training_F
WHERE country = 'mexico' AND block = 'family' AND "table" = 'FAD'
QUALIFY delta_days < 0
ORDER BY bulletin_date DESC;
