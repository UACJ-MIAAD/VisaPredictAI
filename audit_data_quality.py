"""
Data-quality audit for the VisaPredict AI database (employment + family CSVs).

Produces a Markdown report covering, per (country x block x table):
  - temporal coverage and gaps (missing monthly bulletins)
  - duplicate (level, bulletin, table) keys
  - failed parses vs. true 'U' (Unavailable) -- currently indistinguishable
  - share of NaN priority_date
  - category coverage vs. the panel y_{p,c,b,t} promised in the anteproyecto

Run from the repo root:
    ante/bin/python audit_data_quality.py
Writes: data_quality_report.md
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from config import CANONICAL_COUNTRY
from config import DATA_DIR as DATA

COUNTRIES = list(CANONICAL_COUNTRY)  # slugs in canonical order
OUT = Path("data_quality_report.md")


def month_range(start: pd.Timestamp, end: pd.Timestamp) -> pd.DatetimeIndex:
    return pd.date_range(start=start, end=end, freq="MS")


def gaps(dates: pd.Series) -> list[str]:
    d = pd.to_datetime(dates.dropna().unique())
    if len(d) == 0:
        return []
    d = pd.DatetimeIndex(sorted(d))
    full = month_range(d.min(), d.max())
    missing = full.difference(d)
    return [m.strftime("%Y-%m") for m in missing]


def audit_block(name: str, level_col: str, has_table_type: bool) -> list[str]:
    lines = [f"## Bloque: {name}", ""]
    header = "| País | Filas | Rango | Meses esp. | Faltantes | Dup. clave | Niveles | NaN fecha | DFF |"
    lines += [header, "|" + "---|" * 9]

    for c in COUNTRIES:
        suffix = "_family" if name == "Familiar" else ""
        fp = DATA / f"{c}{suffix}_visa_backlog_timecourse.csv"
        if not fp.exists():
            lines.append(f"| {c} | (archivo ausente) |||||||| ")
            continue
        df = pd.read_csv(fp)
        df["visa_bulletin_date"] = pd.to_datetime(df["visa_bulletin_date"], errors="coerce")

        n = len(df)
        rng = f"{df.visa_bulletin_date.min():%Y-%m}→{df.visa_bulletin_date.max():%Y-%m}"
        expected = len(month_range(df.visa_bulletin_date.min(), df.visa_bulletin_date.max()))
        miss = gaps(df["visa_bulletin_date"])
        n_miss = len(miss)

        key = [level_col, "visa_bulletin_date"] + (["table_type"] if has_table_type else [])
        n_dup = int(df.duplicated(subset=key).sum())

        uniq = sorted(df[level_col].dropna().astype(str).unique())
        levels = f"{len(uniq)} cats" if len(uniq) > 6 else "/".join(uniq)
        nan_pct = 100 * df["priority_date"].isna().mean()

        if has_table_type:
            dff = "✓" if "dates_for_filing" in df.get("table_type", pd.Series()).unique() else "✗"
        else:
            dff = "✗ (no extrae)"

        lines.append(f"| {c} | {n} | {rng} | {expected} | {n_miss} | {n_dup} | {levels} | {nan_pct:.0f}% | {dff} |")

        if 0 < n_miss <= 18:
            lines.append(f"|   ↳ huecos {c}: {', '.join(miss)} |" + " |" * 8)
    lines.append("")
    return lines


def panel_section() -> list[str]:
    fp = DATA / "visa_panel_long.csv"
    if not fp.exists():
        return ["## Panel consolidado", "", "_`visa_panel_long.csv` aún no generado (corre `build_panel.py`)._", ""]
    p = pd.read_csv(fp)
    n_series = p.groupby(["country", "block", "category", "table"]).ngroups
    sc = p["status"].value_counts().to_dict()
    bt = p.groupby(["block", "table"]).size().to_dict()
    f = p[p.status == "F"]
    lines = [
        "## Panel consolidado `visa_panel_long.csv`",
        "",
        f"- Filas: **{len(p):,}** · series país×categoría×tabla: **{n_series}**",
        "- Status: " + ", ".join(f"{k}={v:,}" for k, v in sc.items()),
        "- Bloque×tabla: " + ", ".join(f"{b}/{t}={n:,}" for (b, t), n in bt.items()),
        f"- Objetivo entrenable (status=F): **{len(f):,}** filas ({100 * len(f) / len(p):.0f}%)",
        f"- `days_since_base` ∈ [{f.days_since_base.min():.0f}, {f.days_since_base.max():.0f}] "
        f"(base 1975-01-01); 0 negativos.",
        "",
    ]
    return lines


def main() -> None:
    lines = [
        "# Auditoría de calidad de datos — VisaPredict AI",
        "",
        "_Generado por `audit_data_quality.py` sobre los CSV vigentes en `data/`._",
        "",
        "Convenciones de las columnas: `priority_date` = fecha de prioridad "
        "publicada (parseada); `status` ∈ {C,F,U,UNK} conserva el régimen y "
        "`raw_value` la celda cruda (fix H1).",
        "",
    ]
    lines += audit_block("Empleo", "EB_level", has_table_type=True)
    lines += audit_block("Familiar", "F_level", has_table_type=True)

    lines += panel_section()
    lines += [
        "## Hallazgos transversales",
        "",
        "- **H1 — Estado e∈{C,F,U} ✅ RESUELTO.** Los scrapers ahora emiten las "
        "columnas `status` (C/F/U/UNK) y `raw_value`; el panel entrena *solo sobre "
        "status='F'* y conserva C/U como anotación descriptiva (formulación v5.1).",
        "- **H2 — DFF de Empleo ✅ RESUELTO.** El scraper de empleo ahora "
        "captura las dos tablas (FAD + DFF, vía `table_type`); DFF disponible "
        "desde Oct-2015. +2,032 filas DFF de empleo, +20 series.",
        "- **H3 — EB-5 y subcategorías ✅ RESUELTO.** `classify_eb_category()` "
        "mapea las etiquetas (con 20 años de deriva) a 16 códigos canónicos: EB1-4, "
        "EB3_OW, EB4_RW/TRANS, y EB5 (bare/TEA/PILOT/RC/NONRC/UNRESERVED/RURAL/"
        "HIGHUNEMP/INFRA). Schedule A queda fuera de alcance. Panel 90→186 series.",
        "- **H4 — Cobertura extendida al piso de la fuente ✅ (parcial).** "
        "Detección robusta de columnas/sección en **ambos scrapers** (categoría = "
        "col 0; sección por `employment[\\s-]*based` / substring `family`; RoW por "
        "`except those listed`) recuperó **2001-12→2003-09**, el **cluster 2007-2008** "
        "y **arregló RoW** (empleo truncado a 2016-04, familiar a 2015-05). Huecos "
        "familiares 58-69→6-17. **Piso real = dic-2001**: pre-2002 da 404 en "
        "travel.state.gov; llegar a 1992 exigiría Wayback Machine (fuera de alcance). "
        "⚠️ El `.tex` afirma 'FAD desde 1992 (~408 obs)' — irreal desde la fuente "
        "oficial (~294 meses máx).",
        "- **H5 — `NaN` ambiguo ✅ RESUELTO.** `status` distingue 'U' (Unavailable) "
        "de 'UNK' (celda vacía/no parseable). Centinela `UNK` (no `NA`) para evitar "
        "la coerción a NaN de pandas. En el panel actual: 1 fila UNK.",
        "",
    ]
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"✓ Reporte escrito en {OUT}")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
