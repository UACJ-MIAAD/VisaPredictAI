"""
Consolidate the 10 per-country CSVs (employment + family) into the single long
panel  y_{p,c,b,t}  promised by the VisaPredict AI anteproyecto:

    p = país o área de cargabilidad
    c = categoría migratoria  (EB1..EB4 / F1,F2A,F2B,F3,F4)
    b = tipo de tabla         (FAD / DFF)
    t = mes del boletín

Dependent variable: `days_since_base` = días desde una fecha base fija
(`BASE`), calculado **únicamente sobre observaciones con status 'F'** (una
fecha de prioridad específica). Las celdas 'C' (Current) y 'U' (Unavailable)
se conservan como anotación descriptiva en `status` / `raw_value` pero **no**
son objetivo predictivo (formulación v5.1).

Run from the repo root (after the scrapers have written `status`/`raw_value`):
    ante/bin/python -m pipeline.build_panel
Writes: data/processed/visa_panel_long.csv
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from vp_data.config import BASE_EPOCH, TABLE_MAP
from vp_data.config import CANONICAL_COUNTRY as COUNTRIES
from vp_data.config import PANEL_PATH as OUT
from vp_data.config import RAW_DIR as RAW

logger = logging.getLogger(__name__)
BASE = pd.Timestamp(BASE_EPOCH)

PANEL_COLS = [
    "country",
    "block",
    "category",
    "table",
    "bulletin_date",
    "status",
    "priority_date",
    "days_since_base",
    "raw_value",
]


def _require(df: pd.DataFrame, fp: Path, level_col: str) -> None:
    # O3: validate EVERY column the code actually indexes (a rename upstream used
    # to die with a raw KeyError instead of this actionable message).
    missing = {"status", "raw_value", "table_type", "priority_date", "visa_bulletin_date", level_col} - set(df.columns)
    if missing:
        raise SystemExit(
            f"{fp} no tiene las columnas {missing}. "
            f"Re-ejecuta los scrapers (que ya emiten status/raw_value/table_type) antes de build_panel."
        )
    # K1: un CSV fuente solo-headers pasaba la validación de columnas y pd.concat
    # lo tragaba sin ruido — un país entero desaparecía del panel con todos los
    # gates en verde (probado empíricamente en la auditoría). Cero filas de datos
    # en una fuente = regresión del parser, jamás un estado válido.
    if df.empty:
        raise SystemExit(f"{fp} tiene 0 filas de datos — regresión del scraper; se aborta sin escribir el panel.")


def load_employment() -> pd.DataFrame:
    frames = []
    for slug, canon in COUNTRIES.items():
        fp = RAW / f"{slug}_visa_backlog_timecourse.csv"
        # O3: keep_default_na=False en las columnas de texto — un literal "NA" en
        # status/raw_value se coercionaba a NaN y borraba la anotación que el
        # centinela UNK existe para proteger.
        df = pd.read_csv(fp, dtype={"status": str, "raw_value": str}, keep_default_na=False, na_values=[""])
        _require(df, fp, "EB_level")
        df = df.rename(columns={"visa_bulletin_date": "bulletin_date"})  # priority_date already named
        df["country"] = canon
        df["block"] = "employment"
        df["category"] = df["EB_level"].astype(str)  # ya es código canónico EB1..EB5_*
        df["table"] = df["table_type"].map(TABLE_MAP)
        if df["table"].isna().any():  # H2: un table_type nuevo debe explotar, no mapear a NaN
            raise SystemExit(
                f"table_type desconocido: {sorted(df.loc[df.table.isna(), 'table_type'].unique())}"
            )  # FAD + DFF (DFF desde Oct-2015)
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


def load_family() -> pd.DataFrame:
    frames = []
    for slug, canon in COUNTRIES.items():
        fp = RAW / f"{slug}_family_visa_backlog_timecourse.csv"
        df = pd.read_csv(fp, dtype={"status": str, "raw_value": str}, keep_default_na=False, na_values=[""])
        _require(df, fp, "F_level")
        df = df.rename(columns={"visa_bulletin_date": "bulletin_date"})  # priority_date already named
        df["country"] = canon
        df["block"] = "family"
        df["category"] = "F" + df["F_level"].astype(str)
        df["table"] = df["table_type"].map(TABLE_MAP)
        if df["table"].isna().any():  # H2: un table_type nuevo debe explotar, no mapear a NaN
            raise SystemExit(f"table_type desconocido: {sorted(df.loc[df.table.isna(), 'table_type'].unique())}")
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


def main() -> None:
    panel = pd.concat([load_employment(), load_family()], ignore_index=True)

    # format='mixed': the source CSVs mix "YYYY-MM-DD" (employment) and
    # "YYYY-MM-DD HH:MM:SS" (family); a single inferred format would coerce the
    # minority to NaT, so parse each value on its own.
    panel["bulletin_date"] = pd.to_datetime(panel["bulletin_date"], errors="coerce", format="mixed")
    panel["priority_date"] = pd.to_datetime(panel["priority_date"], errors="coerce", format="mixed")

    # H2: una fecha F malformada coercionada a NaT violaría days_iff_F LEJOS de la causa
    # (en el CHECK de DuckDB) — abortar aquí con las filas culpables.
    bad_f = panel[(panel["status"] == "F") & panel["priority_date"].isna()]
    if not bad_f.empty:
        ex = bad_f[["country", "category", "table", "bulletin_date", "raw_value"]].head(5)
        raise SystemExit(f"{len(bad_f)} filas status=F con priority_date imparseable:\n{ex}")

    # The dependent variable lives ONLY on status 'F'. For C/U/NA the priority
    # date carries no predictive meaning (C was flattened to the bulletin date
    # upstream), so null it out and leave days_since_base undefined.
    not_f = panel["status"] != "F"
    panel.loc[not_f, "priority_date"] = pd.NaT
    panel["days_since_base"] = (panel["priority_date"] - BASE).dt.days

    # Base-epoch guard. strptime's 2-digit-year pivot maps '69'..'99' to
    # 1969..1999, so a genuine 1969-1974 priority date would land before the
    # t0=1975 epoch and make days_since_base negative — which the DuckDB CHECK
    # (days_since_base >= 0) rejects deep in the DB load. Fail here instead, with
    # an actionable message: a date this old means BASE_EPOCH (the thesis t0) must
    # be revisited, not silently truncated.
    underflow = panel[(panel.status == "F") & (panel.days_since_base < 0)]
    if not underflow.empty:
        worst = underflow.priority_date.min()
        raise SystemExit(
            f"{len(underflow)} fechas F anteriores a BASE={BASE:%Y-%m-%d} "
            f"(la más antigua: {worst:%Y-%m-%d}). Revisar BASE_EPOCH (t0 de la tesis) en config.py."
        )

    # Integer dtype (nullable): the dependent variable is a day count, not a
    # float. NaT rows coerce the raw subtraction to float64; Int64 restores the
    # integer contract so the CSV writes "18262", not "18262.0".
    panel["days_since_base"] = panel["days_since_base"].astype("Int64")

    panel = (
        panel[PANEL_COLS].sort_values(["country", "block", "category", "table", "bulletin_date"]).reset_index(drop=True)
    )

    # Defensive: the same canonical category can appear twice in one bulletin
    # during a label transition (e.g. the May-2022 EB-5 'Unreserved' split).
    # O2: 'first' was arbitrary with respect to the regime — if the U-labeled
    # duplicate came first in the source table, a trainable F observation was
    # silently dropped. Prefer F > C > U > UNK, and abort if two Fs disagree
    # (same series, same month, different published dates = source conflict a
    # human must resolve, not a coin flip).
    key = ["country", "block", "category", "table", "bulletin_date"]
    if panel.duplicated(subset=key).any():
        conflict = panel[panel.status == "F"].groupby(key)["priority_date"].nunique()
        if (conflict > 1).any():
            raise SystemExit(
                f"claves con DOS fechas F distintas en el mismo boletín: {conflict[conflict > 1].index.tolist()[:5]}"
            )
        rank = panel["status"].map({"F": 0, "C": 1, "U": 2, "UNK": 3})
        order = panel.assign(_rank=rank).sort_values([*key, "_rank"])
        dup = order.duplicated(subset=key, keep="first")
        logger.warning("%d filas duplicadas por clave colapsadas (preferencia F>C>U>UNK)", int(dup.sum()))
        panel = order[~dup].drop(columns="_rank").sort_values(key).reset_index(drop=True)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    panel.to_csv(OUT, index=False)

    # ---- summary -----------------------------------------------------------
    n_series = panel.groupby(["country", "block", "category", "table"]).ngroups
    f = panel[panel.status == "F"]
    logger.info(f"Panel escrito en {OUT}")
    logger.info(f"  filas totales      : {len(panel):,}")
    logger.info(f"  series (p×c×b)      : {n_series}")
    logger.info(f"  rango temporal      : {panel.bulletin_date.min():%Y-%m} → {panel.bulletin_date.max():%Y-%m}")
    logger.info(f"  filas por status   : {panel['status'].value_counts().to_dict()}")
    logger.info(f"  filas por bloque×tabla: {panel.groupby(['block', 'table']).size().to_dict()}")
    logger.info(f"  objetivo entrenable (status F): {len(f):,} filas ({100 * len(f) / len(panel):.0f}% del panel)")
    logger.info(
        f"  days_since_base rango: [{f.days_since_base.min():.0f}, {f.days_since_base.max():.0f}] "
        f"(base = {BASE:%Y-%m-%d})"
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main()
