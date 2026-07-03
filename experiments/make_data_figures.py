"""Figuras de la sección de DATOS/EDA del entregable LaTeX (PI-I): fuente, conversión, CSV, BD.

Genera, en reports/latex/Figures/ (paleta UACJ + serif):
  data_source_page.png     recorte del screenshot real de travel.state.gov (la fuente)
  data_bulletin_table.pdf  la tabla FAD familiar real (jul-2026) tal como se publica, con régimen
  data_csv_panel.pdf       muestra del panel tidy visa_panel_long.csv (estados C/F/U)
  data_duckdb_sample.pdf   muestra del almacén DuckDB (vista v_panel_long) + las 11 tablas
  data_schema_er.png       recorte del diagrama ER del esquema estrella

Insumos: /tmp/vb_month.png, /tmp/schema_er.png (capturas con Chrome), data/processed/*.
Corre en `ante`:  ante/bin/python make_data_figures.py
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from PIL import Image  # noqa: E402

from vp_model.palette import BLUE, GRAY, MUTE, REGIME, STRIPE  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIG = ROOT / "reports" / "latex" / "Figures"
# regímenes de celda (relleno pastel) desde la paleta canónica
DATEC, CURC, UNAC, UNKC = REGIME["F"]["fill"], REGIME["C"]["fill"], REGIME["U"]["fill"], REGIME["UNK"]["fill"]
plt.rcParams.update({"font.family": "serif", "savefig.bbox": "tight", "savefig.dpi": 300})

AREAS = ["all_chargeability", "china", "india", "mexico", "philippines"]
ANAMES = ["All Charg.", "China", "India", "México", "Filipinas"]
CATS = ["F1", "F2A", "F2B", "F3", "F4"]


def crop_source() -> None:
    """Recorta el screenshot del Visa Bulletin a la cabecera + primera tabla (sin la barra de nav)."""
    im = Image.open("/tmp/vb_month.png")
    w, h = im.size
    im.crop((0, 250, w, 1700)).save(FIG / "data_source_page.png")  # zona con título + tablas
    print("data_source_page.png OK")


def trim_er() -> None:
    im = Image.open("/tmp/schema_er.png").convert("RGB")
    bg = Image.new("RGB", im.size, (255, 255, 255))
    from PIL import ImageChops

    diff = ImageChops.difference(im, bg)
    bbox = diff.getbbox()
    if bbox:
        pad = 12
        bbox = (
            max(0, bbox[0] - pad),
            max(0, bbox[1] - pad),
            min(im.size[0], bbox[2] + pad),
            min(im.size[1], bbox[3] + pad),
        )
        im = im.crop(bbox)
    im.save(FIG / "data_schema_er.png")
    print("data_schema_er.png OK")


def _regime_color(v: str) -> str:
    s = str(v).strip().upper()
    if s == "C":
        return CURC
    if s == "U":
        return UNAC
    if s in ("", "UNK", "NAN"):
        return UNKC
    return DATEC


def fig_bulletin_table() -> None:
    """Tabla FAD de empleo real de jul-2026 (muestra los tres regímenes C/F/U que publica el boletín)."""
    d = pd.read_csv(ROOT / "data" / "processed" / "visa_panel_long.csv")
    cats = ["EB1", "EB2", "EB3", "EB4", "EB5_UNRESERVED"]
    labels = ["EB-1", "EB-2", "EB-3", "EB-4", "EB-5*"]
    g = d[(d.bulletin_date == "2026-07-01") & (d.table == "FAD") & (d.block == "employment")]
    piv = g.pivot_table(index="category", columns="country", values="raw_value", aggfunc="first").reindex(cats)[AREAS]
    fig, ax = plt.subplots(figsize=(7.0, 2.5))
    ax.axis("off")
    ncol = len(AREAS) + 1
    ax.add_patch(plt.Rectangle((0, len(cats)), ncol, 1, color=BLUE))
    ax.text(0.5, len(cats) + 0.5, "Cat.", ha="center", va="center", color="white", fontweight="bold", fontsize=9)
    for j, an in enumerate(ANAMES):
        ax.text(j + 1.5, len(cats) + 0.5, an, ha="center", va="center", color="white", fontweight="bold", fontsize=8.5)
    for i, (cat, lab) in enumerate(zip(cats, labels, strict=True)):
        y = len(cats) - 1 - i
        ax.add_patch(plt.Rectangle((0, y), 1, 1, facecolor=BLUE, alpha=0.10, edgecolor="white"))
        ax.text(0.5, y + 0.5, lab, ha="center", va="center", fontsize=8.5, fontweight="bold")
        for j, area in enumerate(AREAS):
            v = piv.loc[cat, area]
            ax.add_patch(plt.Rectangle((j + 1, y), 1, 1, facecolor=_regime_color(v), edgecolor="white"))
            ax.text(j + 1.5, y + 0.5, str(v), ha="center", va="center", fontsize=8, family="monospace")
    ax.set_xlim(0, ncol)
    ax.set_ylim(0, len(cats) + 1)
    # leyenda de régimen
    from matplotlib.patches import Patch

    leg = [
        Patch(facecolor=DATEC, edgecolor=GRAY, label="Fecha (F)"),
        Patch(facecolor=CURC, edgecolor=GRAY, label="Current (C)"),
        Patch(facecolor=UNAC, edgecolor=GRAY, label="Unavailable (U)"),
    ]
    ax.legend(handles=leg, loc="lower center", bbox_to_anchor=(0.5, -0.18), ncol=3, fontsize=7.5, frameon=False)
    fig.savefig(FIG / "data_bulletin_table.pdf")
    plt.close(fig)
    print("data_bulletin_table.pdf OK")


def _render_df(df: pd.DataFrame, path: Path, title: str) -> None:
    df = df.fillna("").replace({"nan": "", "NaT": ""}).astype(str)
    fig, ax = plt.subplots(figsize=(7.6, 0.34 * (len(df) + 1.8)))
    ax.axis("off")
    tbl = ax.table(cellText=df.values, colLabels=df.columns, loc="center", cellLoc="center")
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(7.2)
    tbl.auto_set_column_width(col=list(range(len(df.columns))))  # ancho por contenido (evita truncar)
    tbl.scale(1, 1.30)
    for (r, _c), cell in tbl.get_celld().items():
        cell.set_edgecolor(MUTE)
        cell.set_linewidth(0.5)
        if r == 0:
            cell.set_facecolor(BLUE)
            cell.set_text_props(color="white", fontweight="bold")
        elif r % 2 == 0:
            cell.set_facecolor(STRIPE)
    ax.set_title(title, fontsize=9.5, color=BLUE, fontweight="bold", pad=6)
    fig.savefig(path)
    plt.close(fig)


def fig_csv_panel() -> None:
    """Muestra del panel tidy: filas que ilustran los tres regímenes C/F/U."""
    d = pd.read_csv(ROOT / "data" / "processed" / "visa_panel_long.csv")
    sub = d[(d.country == "mexico") & (d.category == "F2A") & (d.table == "FAD")].copy()
    rows = pd.concat([sub[sub.status == "C"].head(2), sub[sub.status == "F"].head(4)])
    extra_u = d[d.status == "U"].head(1)
    extra = d[(d.country == "india") & (d.category == "F1") & (d.table == "FAD") & (d.status == "F")].head(2)
    show = pd.concat([rows, extra, extra_u]).copy()
    show["country"] = show["country"].replace({"all_chargeability": "all_charg", "mexico": "México", "india": "India"})
    show = show[
        ["country", "category", "table", "bulletin_date", "status", "priority_date", "days_since_base", "raw_value"]
    ]
    show.columns = ["país", "cat.", "tabla", "mes (t)", "estado", "fecha prioridad", "días_base", "celda cruda"]
    for c in ("mes (t)", "fecha prioridad"):
        show[c] = show[c].astype(str).str.slice(0, 10).replace("nan", "")
    show["días_base"] = show["días_base"].apply(lambda x: "" if pd.isna(x) or x == "" else str(int(float(x))))
    _render_df(
        show, FIG / "data_csv_panel.pdf", "Panel tidy:  visa_panel_long.csv   (variable objetivo days_since_base)"
    )
    print("data_csv_panel.pdf OK")


def fig_duckdb() -> None:
    """Muestra del almacén DuckDB: consulta SQL a la vista gold v_panel_long (serie entrenable F)."""
    con = duckdb.connect(str(ROOT / "data" / "processed" / "visapredict.duckdb"), read_only=True)
    q = """SELECT country AS pais, category AS cat, "table" AS tabla,
                  strftime(bulletin_date,'%Y-%m') AS mes, status AS estado,
                  strftime(priority_date,'%Y-%m-%d') AS fecha_prioridad,
                  days_since_base AS dias_base
           FROM v_panel_long
           WHERE country='mexico' AND category='F3' AND "table"='FAD' AND status='F'
           ORDER BY bulletin_date DESC LIMIT 8"""
    df = con.execute(q).fetchdf()
    con.close()
    df["pais"] = "México"
    df["dias_base"] = df["dias_base"].apply(lambda x: "" if pd.isna(x) else str(int(x)))
    _render_df(df, FIG / "data_duckdb_sample.pdf", "Almacén DuckDB (capa gold):  SELECT ... FROM v_panel_long")
    print("data_duckdb_sample.pdf OK")


if __name__ == "__main__":
    crop_source()
    trim_er()
    fig_bulletin_table()
    fig_csv_panel()
    fig_duckdb()
    print("Figuras de datos en", FIG)
