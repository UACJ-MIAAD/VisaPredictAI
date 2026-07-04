"""Censo estadístico EDA del panel completo -> reports/eda/eda_facts.json.

El EDA deja de ser "25 series y anécdotas": este script deriva de los datos un censo
machine-readable de las 194 series estructurales (perfil, régimen, retrogresiones,
congelamiento) + pruebas formales (ADF/KPSS/DF-GLS, Ljung-Box, ARCH) sobre las series
evaluables + agregados que consumen la galería de figuras, el reporte PDF
(``build_eda_report.py``), la web (sección #eda) y las tablas del .tex.

Reglas: 0 cifras a mano (todo derivado del panel CSV, el MISMO insumo que
``build_key_facts.py`` — los conteos compartidos DEBEN coincidir, hay test);
gate de salida C2 (censo incompleto => no publica).

Uso (ante):  ante/bin/python experiments/build_eda_facts.py   (o `make eda-facts`)
"""

from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

import pandas as pd

from vp_model.dataset import is_evaluable
from vp_model.eda import stationarity_of

ROOT = Path(__file__).resolve().parent.parent
PANEL = ROOT / "data" / "processed" / "visa_panel_long.csv"
DV = ROOT / "data" / "raw" / "dv_visa_rank_timecourse.csv"
OUT = ROOT / "reports" / "eda" / "eda_facts.json"

# Gate C2: el censo debe cubrir >=90% de las series estructurales esperadas; un
# entorno a medias no publica un censo mutilado (protege web/PDF/tex aguas abajo).
MIN_CENSUS_FRACTION = 0.9
EXPECTED_STRUCTURAL = 190  # piso duro, alineado al gate de panel (>=190 series)


def _series_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Una fila por serie estructural con perfil + régimen + retro + congelamiento."""
    rows = []
    for (country, block, category, table), g in df.groupby(["country", "block", "category", "table"]):
        g = g.sort_values("bulletin_date")
        f = g[g.status == "F"]
        rec: dict = {
            "country": country,
            "block": block,
            "category": category,
            "table": table,
            "n_total": int(len(g)),
            "n_F": int(len(f)),
            "n_C": int((g.status == "C").sum()),
            "n_U": int((g.status == "U").sum()),
            "n_UNK": int((g.status == "UNK").sum()),
        }
        if len(f):
            per = f.bulletin_date.dt.to_period("M")
            span = int((per.max() - per.min()).n + 1)
            y = f.days_since_base.astype("float64").reset_index(drop=True)
            d = y.diff().dropna()
            rec.update(
                first_F=f.bulletin_date.min().strftime("%Y-%m"),
                last_F=f.bulletin_date.max().strftime("%Y-%m"),
                span_months=span,
                continuity=round(len(f) / span, 4),
                n_gaps=span - len(f),
                n_retro=int((d < 0).sum()),
                worst_retro_days=int(-d.min()) if len(d) and d.min() < 0 else 0,
                median_step_days=round(float(d.median()), 1) if len(d) else 0.0,
                pct_frozen=round(float((d == 0).mean()), 4) if len(d) else 0.0,
                evaluable=bool(is_evaluable(len(f), span, table)),
            )
        else:
            rec.update(span_months=0, continuity=0.0, n_gaps=0, n_retro=0, evaluable=False)
        rows.append(rec)
    return pd.DataFrame(rows)


def _formal_tests(df: pd.DataFrame, census: pd.DataFrame) -> pd.DataFrame:
    """ADF/KPSS/DF-GLS + Ljung-Box + ARCH + forma, SOLO sobre series evaluables.

    Opera sobre las observaciones F del panel (la serie que ve el modelado); las
    pruebas de ruido/heteroscedasticidad van sobre los incrementos (la serie en
    nivel es I(1) casi siempre y las invalidaría).

    AB3 (decisión documentada, docs/CLEANING.md stationarity_on_raw_F): se corre
    sobre las F CRUDAS sin imputar — imputar antes de una prueba de raíz unitaria
    sesga el veredicto hacia 'integrada'. Costo aceptado: en series con huecos el
    índice queda comprimido (meses no adyacentes tratados como rezagos contiguos);
    la estructura de rezagos de ADF/KPSS asume espaciado regular. Las evaluables
    tienen continuidad alta, así que la compresión es marginal.
    """
    from scipy import stats as sps
    from statsmodels.stats.diagnostic import acorr_ljungbox, het_arch

    out = []
    todo = census[census.evaluable]
    for i, r in enumerate(todo.itertuples(), 1):
        f = df[
            (df.country == r.country)
            & (df.block == r.block)
            & (df.category == r.category)
            & (df.table == r.table)
            & (df.status == "F")
        ].sort_values("bulletin_date")
        y = pd.Series(f.days_since_base.astype("float64").to_numpy(), index=pd.DatetimeIndex(f.bulletin_date))
        d = y.diff().dropna()
        # Blindaje por serie (audit 3-jul M1): una serie casi-constante (empleo DFF,
        # 77% congelado) puede reventar KPSS/DF-GLS/ARCH con LinAlgError cuando el
        # cohort evaluable crezca. Un fallo puntual NO debe tumbar el censo del mes:
        # se publica con veredicto centinela "failed" y el resto sigue.
        try:
            st = stationarity_of(y)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                lb_p = float(acorr_ljungbox(d, lags=[12], return_df=True).lb_pvalue.iloc[0])
                arch_p = float(het_arch(d - d.mean(), nlags=12)[1])
            rec = {
                "adf_p": st["adf_pvalue"],
                "kpss_p": st["kpss_pvalue"],
                "dfgls_p": st["dfgls_pvalue"],
                "verdict": st["verdict"],
                "lb_p": round(lb_p, 4),
                "arch_p": round(arch_p, 4),
                "skew": round(float(sps.skew(d)), 2),
                "kurtosis": round(float(sps.kurtosis(d, fisher=False)), 1),
            }
        except Exception as exc:  # noqa: BLE001 — el censo degrada, no explota
            print(f"WARN: pruebas formales fallaron en {r.country}/{r.category}/{r.table}: {exc}", file=sys.stderr)
            rec = {k: float("nan") for k in ("adf_p", "kpss_p", "dfgls_p", "lb_p", "arch_p", "skew", "kurtosis")}
            rec["verdict"] = "failed"
        out.append({"country": r.country, "block": r.block, "category": r.category, "table": r.table, **rec})
        if i % 20 == 0:
            print(f"  pruebas formales {i}/{len(todo)}", file=sys.stderr)
    return pd.DataFrame(out)


def _retro_events(df: pd.DataFrame) -> list[dict]:
    """Todos los meses de retrogresión del panel (insumo de la figura-timeline G4)."""
    f = df[df.status == "F"].sort_values("bulletin_date").copy()
    f["delta"] = f.groupby(["country", "block", "category", "table"])["days_since_base"].diff()
    ev = f[f.delta < 0]
    return [
        {
            "date": r.bulletin_date.strftime("%Y-%m"),
            "country": r.country,
            "block": r.block,
            "category": r.category,
            "table": r.table,
            "days": int(-r.delta),
        }
        for r in ev.itertuples()
    ]


def _fad_dff_gap(df: pd.DataFrame) -> list[dict]:
    """Brecha DFF-FAD (días) por serie en el último mes donde ambas tablas son F (G5)."""
    f = df[df.status == "F"]
    wide = f.pivot_table(
        index=["country", "block", "category", "bulletin_date"],
        columns="table",
        values="days_since_base",
        aggfunc="first",
    ).dropna()
    if wide.empty:
        return []
    out = []
    for (country, block, category), g in wide.groupby(level=[0, 1, 2]):
        last = g.iloc[-1]
        out.append(
            {
                "country": country,
                "block": block,
                "category": category,
                "date": g.index.get_level_values(3).max().strftime("%Y-%m"),
                "gap_days": int(last["DFF"] - last["FAD"]),
            }
        )
    return out


def _backlog_today(df: pd.DataFrame) -> list[dict]:
    """Atraso vigente (años) por serie en el último boletín: insumo de G3 y la web."""
    f = df[(df.status == "F") & df.priority_date.notna()]
    last = f.bulletin_date.max()
    row = f[f.bulletin_date == last]
    return [
        {
            "country": r.country,
            "block": r.block,
            "category": r.category,
            "table": r.table,
            "backlog_years": round((r.bulletin_date - r.priority_date).days / 365.25, 1),
        }
        for r in row.itertuples()
    ]


def _monthly_advance(df: pd.DataFrame) -> dict[str, float]:
    """Avance mediano por mes calendario (todo el panel F): insumo de G6."""
    f = df[df.status == "F"].sort_values("bulletin_date").copy()
    f["delta"] = f.groupby(["country", "block", "category", "table"])["days_since_base"].diff()
    med = f.dropna(subset=["delta"]).groupby(f.bulletin_date.dt.month)["delta"].median()
    return {str(m): round(float(v), 1) for m, v in med.items()}


def _dv() -> dict:
    if not DV.exists():
        return {}
    dv = pd.read_csv(DV, parse_dates=["visa_bulletin_date"])
    return {
        "n_rows": int(len(dv)),
        "n_regions": int(dv.region.nunique()),
        "date_first": dv.visa_bulletin_date.min().strftime("%Y-%m"),
        "date_last": dv.visa_bulletin_date.max().strftime("%Y-%m"),
    }


def build() -> dict:
    df = pd.read_csv(PANEL, parse_dates=["bulletin_date", "priority_date"])
    census = _series_frame(df)

    n_structural = len(census)
    if n_structural < MIN_CENSUS_FRACTION * EXPECTED_STRUCTURAL:
        raise SystemExit(
            f"GATE EDA: censo con {n_structural} series (< {MIN_CENSUS_FRACTION:.0%} de "
            f"{EXPECTED_STRUCTURAL}); NO se publica un censo mutilado."
        )

    tests = _formal_tests(df, census)
    census = census.merge(tests, on=["country", "block", "category", "table"], how="left")

    f = df[df.status == "F"].sort_values("bulletin_date").copy()
    f["delta"] = f.groupby(["country", "block", "category", "table"])["days_since_base"].diff()
    deltas = f.delta.dropna()

    facts = {
        "_source": "experiments/build_eda_facts.py — NO editar a mano",
        "vintage": df.bulletin_date.max().strftime("%Y-%m"),
        "panel": {
            "n_obs": int(len(df)),
            "n_series_structural": int(n_structural),
            "n_series_with_F": int((census.n_F >= 1).sum()),
            "n_series_evaluable": int(census.evaluable.sum()),
            "n_obs_F": int((df.status == "F").sum()),
            "pct_trainable_F": int(round(100 * (df.status == "F").mean())),
            "n_months": int(df.bulletin_date.dt.to_period("M").nunique()),
            "date_first": df.bulletin_date.min().strftime("%Y-%m"),
            "date_last": df.bulletin_date.max().strftime("%Y-%m"),
            "pct_retro": round(100 * float((deltas < 0).mean()), 2),
            "pct_frozen": round(100 * float((deltas == 0).mean()), 2),
            "median_step_days": round(float(deltas.median()), 1),
        },
        "regime": {s: int((df.status == s).sum()) for s in ("F", "C", "U", "UNK")},
        "stationarity_summary": {str(v): int((tests.verdict == v).sum()) for v in sorted(tests.verdict.unique())},
        "series": json.loads(census.to_json(orient="records")),
        "retro_events": _retro_events(df),
        "fad_dff_gap": _fad_dff_gap(df),
        "backlog_today": _backlog_today(df),
        "monthly_advance_median": _monthly_advance(df),
        "dv": _dv(),
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(facts, indent=1, ensure_ascii=False) + "\n")
    return facts


if __name__ == "__main__":
    facts = build()
    p = facts["panel"]
    print(
        f"eda_facts OK — vintage {facts['vintage']} · {p['n_series_structural']} series "
        f"({p['n_series_evaluable']} evaluables) · {p['n_obs']} obs · "
        f"{len(facts['retro_events'])} retrogresiones · verdicts {facts['stationarity_summary']}"
    )
