-- 002 — run/row provenance (H2) + deterministic timestamps (H4).
--
-- The warehouse is rebuilt wholesale into a fresh temp file, so every table this
-- migration reshapes is still EMPTY when it runs (the loader inserts only after
-- the whole migration chain is applied). DuckDB 1.5.3 does not support
-- ALTER TABLE ADD COLUMN with constraints ("not yet supported"), so reshaped
-- tables are DROP + CREATE — content-neutral on an empty warehouse, and the
-- atomic tmp + os.replace build keeps the previous database intact if anything
-- here fails.
--
-- TIMESTAMP DETERMINISM CONTRACT (H4): created_at/updated_at on data tables are
-- DERIVED FROM THE DATA (the row's bulletin month / the build's panel vintage),
-- never from the wall clock, so two rebuilds of the same inputs produce
-- byte-identical column values. Real wall-clock time is allowed ONLY in the
-- build-log tables (etl_run, schema_version.applied_at) — the bitácora — and
-- never contaminates the facts.

-- ─────────────── schema_version: migration bookkeeping (H1) ───────────────
-- One row PER APPLIED MIGRATION (was: a single hand-bumped row). applied_at is
-- bitácora (wall-clock, UTC); checksum pins the exact file text that was
-- applied, so a silent edit of an already-applied migration aborts the next
-- build (fail-closed, verified against the previous live database).
DROP TABLE schema_version;
CREATE TABLE schema_version (
    version      INTEGER      PRIMARY KEY,
    description  VARCHAR      NOT NULL,
    applied_at   TIMESTAMPTZ  NOT NULL,
    checksum     VARCHAR      NOT NULL CHECK (length(checksum) = 64)
);

-- ─────────────── etl_run: full identity of the build (H2) ─────────────────
-- Identity values arrive from the loader via CLI/env (never invented in SQL);
-- when unavailable they are NULL, honestly. started_at/completed_at/built_at_utc
-- are bitácora (real wall-clock) — allowed here, NEVER in fact tables (H4).
-- build_status records degradation (missing alias lineage / DV / source
-- snapshots) instead of letting a degraded warehouse look valid.
DROP TABLE etl_run;
CREATE TABLE etl_run (
    run_id           INTEGER      PRIMARY KEY,
    built_at_utc     TIMESTAMPTZ  NOT NULL,
    schema_version   INTEGER      NOT NULL REFERENCES schema_version(version),
    n_fact_priority  INTEGER      NOT NULL,
    n_fact_dv        INTEGER      NOT NULL,
    n_trainable_f    INTEGER      NOT NULL,
    pct_trainable    DOUBLE       NOT NULL CHECK (pct_trainable BETWEEN 0 AND 1),
    panel_floor      DATE         NOT NULL,
    panel_ceiling    DATE         NOT NULL,
    pipeline_run_id  VARCHAR,
    git_sha          VARCHAR      CHECK (git_sha IS NULL OR length(git_sha) = 40),
    git_dirty        BOOLEAN,
    panel_sha256     VARCHAR      CHECK (panel_sha256 IS NULL OR length(panel_sha256) = 64),
    dvc_lock_sha256  VARCHAR      CHECK (dvc_lock_sha256 IS NULL OR length(dvc_lock_sha256) = 64),
    env_lock_sha256  VARCHAR      CHECK (env_lock_sha256 IS NULL OR length(env_lock_sha256) = 64),
    started_at       TIMESTAMPTZ,
    completed_at     TIMESTAMPTZ,
    build_status     VARCHAR      NOT NULL CHECK (build_status IN ('ok', 'degraded')),
    degradations     VARCHAR,
    -- Named so a violation reports the invariant: a degraded build must say WHY.
    CONSTRAINT degraded_iff_reasons CHECK ((build_status = 'degraded') = (degradations IS NOT NULL)),
    CONSTRAINT run_started_le_completed CHECK (
        started_at IS NULL OR completed_at IS NULL OR started_at <= completed_at
    )
);

-- ─────────────── source_artifact: the frozen HTML behind every month (H2) ──
-- One row per snapshot in data/snapshots/ whose filename maps to a bulletin
-- month (announcement pages carry no vintage and stay out of provenance scope).
-- url points at the S3 archive — the project's source of truth for raw HTML;
-- the original travel.state.gov href is not persisted at freeze time and is
-- NOT fabricated. Facts join by value: dim_date.bulletin_date = vintage
-- (1 month : N artifacts tolerated — e.g. a revised bulletin).
CREATE TABLE source_artifact (
    source_id           INTEGER      PRIMARY KEY,
    filename            VARCHAR      NOT NULL UNIQUE,
    url                 VARCHAR,
    license             VARCHAR      NOT NULL,
    sha256              VARCHAR      NOT NULL CHECK (length(sha256) = 64),
    vintage             DATE         NOT NULL,
    source_modified_at  TIMESTAMPTZ,           -- upstream mtime: not tracked (S3 sync rewrites it) => NULL honesto
    created_at          TIMESTAMPTZ  NOT NULL, -- derived: the artifact's bulletin month (vintage), UTC
    updated_at          TIMESTAMPTZ  NOT NULL,
    CONSTRAINT src_created_le_updated CHECK (created_at <= updated_at)
);

-- ─────────────── facts: run linkage + deterministic row timestamps ─────────
-- etl_run_id: NO declarative FK on purpose — etl_run is inserted LAST as the
-- completeness sentinel that vp_model.dataset._connect depends on (M2), and a
-- child FK would force the parent row in before the load. Join integrity is
-- asserted post-load by the builder and pinned by pytest.
-- created_at = the row's bulletin month (its first possible appearance in the
-- source). updated_at = created_at unless a rebuild sees the row's content hash
-- change vs the previous warehouse, in which case it advances to the PANEL
-- VINTAGE of the build that introduced the change (max bulletin month) — never
-- to the wall clock.
DROP TABLE fact_priority;
CREATE TABLE fact_priority (
    area_id          INTEGER      NOT NULL REFERENCES dim_area(area_id),
    category_id      INTEGER      NOT NULL REFERENCES dim_category(category_id),
    table_id         INTEGER      NOT NULL REFERENCES dim_table(table_id),
    date_id          INTEGER      NOT NULL REFERENCES dim_date(date_id),
    status           VARCHAR      NOT NULL REFERENCES dim_status(status) CHECK (status IN ('C', 'F', 'U', 'UNK')),
    priority_date    DATE,
    days_since_base  INTEGER      CHECK (days_since_base IS NULL OR days_since_base >= 0),
    raw_value        VARCHAR,
    etl_run_id       INTEGER      NOT NULL,
    created_at       TIMESTAMPTZ  NOT NULL,
    updated_at       TIMESTAMPTZ  NOT NULL,
    PRIMARY KEY (area_id, category_id, table_id, date_id),
    -- The dependent variable and the priority date are defined IFF status='F'.
    CONSTRAINT days_iff_F  CHECK ((status = 'F') = (days_since_base IS NOT NULL)),
    CONSTRAINT pdate_iff_F CHECK ((status = 'F') = (priority_date  IS NOT NULL)),
    -- M5: the arithmetic contract of the dependent variable (t0 = 1975-01-01).
    CONSTRAINT days_is_datediff CHECK (
        days_since_base IS NULL OR days_since_base = datediff('day', DATE '1975-01-01', priority_date)
    ),
    CONSTRAINT fp_created_le_updated CHECK (created_at <= updated_at)
);

DROP TABLE fact_dv_rank;
CREATE TABLE fact_dv_rank (
    region_id    INTEGER      NOT NULL REFERENCES dim_region(region_id),
    date_id      INTEGER      NOT NULL REFERENCES dim_date(date_id),
    status       VARCHAR      NOT NULL REFERENCES dim_status(status) CHECK (status IN ('C', 'F', 'U', 'UNK')),
    rank_cutoff  INTEGER      CHECK (rank_cutoff IS NULL OR rank_cutoff >= 0),
    raw_value    VARCHAR,
    exceptions   VARCHAR,
    etl_run_id   INTEGER      NOT NULL,
    created_at   TIMESTAMPTZ  NOT NULL,
    updated_at   TIMESTAMPTZ  NOT NULL,
    PRIMARY KEY (region_id, date_id),
    -- The rank cut-off is defined IFF a specific number is published (status 'F').
    CONSTRAINT rank_iff_F CHECK ((status = 'F') = (rank_cutoff IS NOT NULL)),
    CONSTRAINT dv_created_le_updated CHECK (created_at <= updated_at)
);

-- ─────────────── alias bridge: envelope timestamps are pure data ───────────
-- created_at/updated_at mirror valid_from/valid_to (the observed envelope):
-- a label's row "changes" exactly when its envelope grows, so the derived
-- timestamps advance with the data and with nothing else.
DROP TABLE dim_category_alias;
CREATE TABLE dim_category_alias (
    alias_id     INTEGER      PRIMARY KEY,
    category_id  INTEGER      NOT NULL REFERENCES dim_category(category_id),
    raw_label    VARCHAR      NOT NULL,
    valid_from   DATE         NOT NULL,
    valid_to     DATE         NOT NULL,
    n_months     INTEGER      NOT NULL CHECK (n_months > 0),
    created_at   TIMESTAMPTZ  NOT NULL,  -- = valid_from (first month observed), UTC
    updated_at   TIMESTAMPTZ  NOT NULL,  -- = valid_to  (last month observed), UTC
    UNIQUE (category_id, raw_label),
    CHECK (valid_from <= valid_to),
    -- P1: n_months counts DISTINCT observed months (envelope, not SCD-2).
    CHECK (n_months <= datediff('month', valid_from, valid_to) + 1),
    CONSTRAINT alias_created_le_updated CHECK (created_at <= updated_at)
);

-- ─────────────── marts: expose freshness (H4) ──────────────────────────────
-- Per-series last_modified_at = the newest row-content vintage in the series.
CREATE OR REPLACE VIEW mart_series_summary AS
SELECT
    a.slug AS country, c.block AS block, c.code AS category, t.code AS "table",
    count(*) AS n_obs,
    count(*) FILTER (WHERE f.status = 'F') AS n_trainable,
    min(d.bulletin_date) AS first_month,
    max(d.bulletin_date) AS last_month,
    count(DISTINCT f.status) AS n_regimes,
    max(f.updated_at) AS last_modified_at
FROM fact_priority f
JOIN dim_area     a ON a.area_id     = f.area_id
JOIN dim_category c ON c.category_id = f.category_id
JOIN dim_table    t ON t.table_id    = f.table_id
JOIN dim_date     d ON d.date_id     = f.date_id
GROUP BY a.slug, c.block, c.code, t.code;
