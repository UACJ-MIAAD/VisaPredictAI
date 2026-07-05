"""EN translations of the master cleaning + FE decisions, keyed by stable id.

Single source of the English decision text. `build_fe_facts.py` attaches these
as ``title_en``/``rationale_en`` into fe_facts.json (so the web #fe section reads
them from data instead of a hand-kept dict) and `build_fe_report.py` renders the
EN PDF from the same map. Spanish is canonical (the registries in
``vp_data.cleaning`` / ``vp_model.feature_builder``); anything missing here falls
back to Spanish. Keep figures STRUCTURAL (epoch, lag count) — never drifting
panel counts, which belong to key_facts/fe_facts, not prose.
"""

from __future__ import annotations

# Traducciones EN de las decisiones (fe_facts las publica en español, el canónico).
# Clave = id de la decisión; fallback al español si aparece una decisión nueva.
DECISIONS_EN: dict[str, dict[str, str]] = {
    "status_regime": {
        "title": "C/F/U/UNK regime as annotation, F as the only target",
        "rationale": "Flattening C→date and U→NaN destroyed the administrative regime. The status column "
        "preserves it; only F cells (a specific date) are a predictive target (v5.1 formulation) and the "
        "evaluation masks everything else (B1).",
    },
    "unk_sentinel": {
        "title": "UNK sentinel (never the string NA)",
        "rationale": "The literal 'NA' collides with pandas.read_csv's default coercion (it reads it as NaN) "
        "and erased the annotation. UNK distinguishes 'no data' from 'Unavailable' and survives any "
        "downstream consumer.",
    },
    "century_pivot": {
        "title": "Century pivot with an epoch guard",
        "rationale": "Cells publish 2-digit years ('01MAY16'); strptime pivots 69..99→19xx. An F date earlier "
        "than t0=1975 would make days_since_base negative: build_panel aborts (underflow) and the warehouse "
        "CHECK days_is_datediff re-verifies the full arithmetic.",
    },
    "footnote_tolerance": {
        "title": "Tolerance to source typos and footnotes",
        "rationale": "Twenty years of bulletins carry footnotes (C*/U*), stray spaces and typos ('4rd'). The "
        "parser normalizes without discarding the month; whatever cannot be parsed stays UNK with its "
        "raw_value intact (nothing is silently corrected: the raw cell is preserved).",
    },
    "dedup_regime_preference": {
        "title": "Deduplication by regime preference F>C>U>UNK",
        "rationale": "During label transitions (e.g. EB-5 'Unreserved' 2022) a canonical category appears "
        "twice in the same month. 'first' was a coin flip that could drop a trainable F observation; F is "
        "preferred and the build ABORTS if two F cells of the same month disagree (a source conflict for a "
        "human to resolve).",
    },
    "date_failfast": {
        "title": "Unparseable dates abort at the cause",
        "rationale": "An F date coerced to NaT would violate days_iff_F far from its cause (in the warehouse "
        "CHECK); a NaT bulletin_date would travel all the way to the dim_date merge. Both abort in "
        "build_panel with the offending rows (AA3).",
    },
    "domain_validation": {
        "title": "Category domains validated on read",
        "rationale": "keep_default_na=False protects the UNK sentinel but disables NA coercion for the whole "
        "frame; a stray literal in F_level/EB_level would pass as a string. The domain is validated "
        "explicitly after every read_csv (AA4).",
    },
    "gap_policy_training": {
        "title": "Gaps: interpolate ≤3 months; long ones NaN; filling only to train",
        "rationale": "Gaps are C/U months (MNAR: the absence itself is signal). Runs of ≤3 months are "
        "linearly interpolated; longer ones stay NaN (all-or-nothing per run, no partial ramps). "
        "to_timeseries fills residual NaNs ONLY to give the training continuity — they are never targets: "
        "the evaluation scores real F dates only (B1 mask, single source metrics._aligned).",
    },
    "eda_kalman": {
        "title": "EDA characterization imputes with Kalman, never unbounded ramps",
        "rationale": "STL/spectrum/catch22 demand complete series. Long gaps are imputed with Kalman "
        "smoothing (state space, imputeTS::na_kalman), not multi-year linear interpolation or edge "
        "extrapolation: an invented ramp fabricates trend and contaminates Hurst/changepoints/entropy (AB1).",
    },
    "stationarity_on_raw_F": {
        "title": "Formal tests on the raw F observations (with a spacing caveat)",
        "rationale": "ADF/KPSS/DF-GLS run on the unimputed F observations: imputing before a unit-root test "
        "biases toward 'integrated'. Accepted, documented cost: in gappy series the index is compressed and "
        "the lag structure assumes regular spacing (AB3).",
    },
    "outliers_as_signal": {
        "title": "Retrogressions = signal; outliers are counted, never trimmed",
        "rationale": "Retrogressions and >8-year jumps are real administrative events the model must "
        "tolerate (the thesis argues this). No step winsorizes or removes extreme values; they are only "
        "COUNTED with robust statistics (STL z-scores, Hampel) and the figures annotate whatever falls out "
        "of range instead of silently clipping it (AC1/AC2).",
    },
    "schema_contract": {
        "title": "The contract is re-verified declaratively in the warehouse",
        "rationale": "Cleaning invariants do not live in Python alone: the star schema's CHECK/PK/FK "
        "constraints reject on load any row that violates them, naming the exact broken invariant.",
    },
    "target_days_since_base": {
        "title": "Target = days since t0 (1975-01-01), F status only",
        "rationale": "The priority date becomes a continuous integer of days since a fixed epoch earlier "
        "than the oldest observed priority (1979-11, Philippines F4). A continuous numeric target, with the "
        "arithmetic contract re-verified in the warehouse, instead of raw dates impossible to regress.",
    },
    "gap_regularization": {
        "title": "Regular monthly grid with bounded gaps",
        "rationale": "The models demand a regular index; C/U months are not targets. Gap runs of ≤3 months "
        "are linearly interpolated; long ones stay NaN (all-or-nothing per run) and the later continuity "
        "fill is never scored (F-only mask B1).",
    },
    "differencing_trees": {
        "title": "Trees predict the first difference, not the level",
        "rationale": "A tree does not extrapolate beyond the range it saw: on the level (decades of rising "
        "trend) it saturates at the historical maximum. Modeling the monthly Δy (stationary) and "
        "reintegrating causally (cumsum anchored at the last observed level) solves extrapolation for free.",
    },
    "calendar_cyclic": {
        "title": "Fiscal calendar encoded cyclically (sine/cosine)",
        "rationale": "The visa fiscal year starts in October (quotas reset there). Encoding the month and "
        "the fiscal position with sine/cosine avoids imposing a false order between December and January — "
        "an integer 1..12 would make the model see those neighboring months as the farthest apart.",
    },
    "lags_24": {
        "title": "24 monthly lags as the regressors' memory",
        "rationale": "Two years of history per origin: covers a full fiscal cycle with margin and leaves "
        "enough degrees of freedom (evaluable series ≥84 F). The constant is externalized in config, not "
        "buried per model.",
    },
    "scaling_leakage_free": {
        "title": "Scaling fitted ONLY on the initial window",
        "rationale": "Torch networks behave poorly on magnitudes of ~18,000 days. The Scaler is fitted "
        "exclusively on the explicit training window and inverted after predicting: fitting it on the full "
        "series would leak the future into the past.",
    },
    "covariate_policy": {
        "title": "Explicit covariate policy per model family",
        "rationale": "Only the differenced trees receive the calendar (the canonical campaign was derived "
        "that way); rlinear and the NNs deliberately go without covariates. 'year' is kept for provenance "
        "of the published figures and is documented as a removal candidate for the next re-campaign.",
    },
    "selection_fresh_mrmr": {
        "title": "FRESH selection (FDR) + mRMR de-redundancy of the catalog",
        "rationale": "With short series (tens to hundreds of observations) every degree of freedom counts. The union set of "
        "characterization features (catch22 + descriptors) is filtered for relevance with "
        "Benjamini-Yekutieli correction and collinearity is collapsed keeping one representative per group "
        "(|Spearman|>0.9), against each series' real forecasting difficulty (champion's MASE).",
    },
}
