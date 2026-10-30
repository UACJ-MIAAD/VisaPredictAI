"""Figuras de la sección de DATOS/EDA del entregable LaTeX (PI-I): fuente, conversión, CSV, BD.

Genera, en reports/latex/Figures/ (paleta UACJ + serif):
  data_source_page.png     recorte del screenshot real de travel.state.gov (la fuente)
  data_bulletin_table.pdf  la tabla FAD familiar real (jul-2026) tal como se publica, con régimen
  data_csv_panel.pdf       muestra del panel tidy visa_panel_long.csv (estados C/F/U)
  data_duckdb_sample.pdf   muestra del almacén DuckDB (vista v_panel_long) + el catálogo de tablas
  data_schema_er.png       diagrama ER del esquema estrella, rasterizado de docs/schema_er.svg

Insumos: docs/schema_er.svg (se rasteriza con Chrome headless, sin paso manual),
/tmp/vb_month.png (captura de la fuente), data/processed/*.
Corre en `ante`:  ante/bin/python make_data_figures.py
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import duckdb
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from PIL import Image

from vp_model.palette import BLUE, COUNTRY_NAME, FONT_RC, GRAY, MUTE, REGIME, STRIPE, cat_label, register_fonts

ROOT = Path(__file__).resolve().parents[1]
FIG = ROOT / "reports" / "latex" / "Figures"
# regímenes de celda (relleno pastel) desde la paleta canónica
DATEC, CURC, UNAC, UNKC = REGIME["F"]["fill"], REGIME["C"]["fill"], REGIME["U"]["fill"], REGIME["UNK"]["fill"]
register_fonts()
plt.rcParams.update({**FONT_RC, "savefig.bbox": "tight", "savefig.dpi": 300})

ER_SVG = ROOT / "docs" / "schema_er.svg"
ER_SCALE = 3.6  # 1360x1000 viewBox x3.6 -> ~4900x3600, print grade


def _er_viewbox() -> tuple[int, int]:
    """Read the SVG's own viewBox. Never hardcode it: two copies of a size drift."""
    head = ER_SVG.read_text(encoding="utf-8")[:2000]
    m = re.search(r'viewBox\s*=\s*["\']\s*[\d.]+\s+[\d.]+\s+([\d.]+)\s+([\d.]+)', head)
    if not m:
        raise SystemExit(f"{ER_SVG}: sin viewBox legible")
    return round(float(m.group(1))), round(float(m.group(2)))


AREAS = ["all_chargeability", "china", "india", "mexico", "philippines"]
ANAMES = ["All Charg.", "China", "India", "México", "Filipinas"]
CATS = ["F1", "F2A", "F2B", "F3", "F4"]


def crop_source() -> None:
    """Recorta el screenshot del Visa Bulletin a la cabecera + primera tabla (sin la barra de nav)."""
    im = Image.open("/tmp/vb_month.png")
    w, h = im.size
    im.crop((0, 250, w, 1700)).save(FIG / "data_source_page.png")  # zona con título + tablas
    print("data_source_page.png OK")


def _render_er_svg(dest: Path) -> bool:
    """Rasterise docs/schema_er.svg with a headless browser. False if none is installed.

    The ER figure used to come from a Chrome screenshot dropped by hand in /tmp,
    so it could not be regenerated -- and it silently kept pre-resurrection row
    counts (27,127 / 15,662) long after the panel grew to 27,611 / 15,931.
    Rendering the versioned SVG makes the PNG a derivative again.
    """
    browsers = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        shutil.which("chromium") or "",
        shutil.which("google-chrome") or "",
    ]
    binary = next((b for b in browsers if b and Path(b).exists()), None)
    if binary is None:
        return False

    svg = ER_SVG
    width, height = _er_viewbox()
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)
        shutil.copy(svg, stage / svg.name)
        (stage / "er.html").write_text(
            "<!doctype html><meta charset='utf-8'>"
            "<style>html,body{margin:0;padding:0;background:#fff}"
            f"img{{display:block;width:{width}px;height:{height}px}}</style>"
            f"<img src='{svg.name}' alt=''>",
            encoding="utf-8",
        )
        subprocess.run(
            [
                binary,
                "--headless",
                "--disable-gpu",
                "--hide-scrollbars",
                "--default-background-color=FFFFFFFF",
                f"--window-size={width},{height}",
                f"--force-device-scale-factor={ER_SCALE}",
                f"--screenshot={dest}",
                (stage / "er.html").as_uri(),
            ],
            check=True,
            capture_output=True,
        )
    return dest.exists()


def trim_er() -> None:
    """Rasterise the star-schema ER diagram and trim its uniform white margin."""
    from PIL import ImageChops

    with tempfile.TemporaryDirectory() as tmp:
        shot = Path(tmp) / "schema_er.png"
        if not _render_er_svg(shot):
            # No browser here: fall back to a screenshot staged by hand.
            shot = Path("/tmp/schema_er.png")
            if not shot.exists():
                raise SystemExit("data_schema_er.png: no headless browser found and no /tmp/schema_er.png staged")
        im = Image.open(shot).convert("RGB")

    bg = Image.new("RGB", im.size, (255, 255, 255))
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


def _render_df(
    df: pd.DataFrame,
    path: Path,
    title: str,
    *,
    width: float = 7.6,
    fontsize: float = 7.2,
    mono_cols: tuple[str, ...] = (),
    row_in: float = 0.34,
) -> None:
    """Tabla tipográfica. ``width``/``fontsize`` permiten dibujarla al ancho de impresión,
    ``row_in`` ajusta el alto del lienzo a la tabla y ``mono_cols`` pone en monoespaciada los
    encabezados que son nombres reales de columna. Los valores por omisión son los de siempre."""
    df = df.fillna("").replace({"nan": "", "NaT": ""}).astype(str)
    fig, ax = plt.subplots(figsize=(width, row_in * (len(df) + 1.8)))
    ax.axis("off")
    tbl = ax.table(cellText=df.values, colLabels=df.columns, loc="center", cellLoc="center")
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(fontsize)
    tbl.auto_set_column_width(col=list(range(len(df.columns))))  # ancho por contenido (evita truncar)
    tbl.scale(1, 1.30)
    for (r, c), cell in tbl.get_celld().items():
        cell.set_edgecolor(MUTE)
        cell.set_linewidth(0.5)
        if r == 0:
            cell.set_facecolor(BLUE)
            cell.set_text_props(color="white", fontweight="bold")
            if df.columns[c] in mono_cols:
                cell.set_text_props(family="monospace")
        elif r % 2 == 0:
            cell.set_facecolor(STRIPE)
    ax.set_title(title, fontsize=fontsize + 2.3, color=BLUE, fontweight="bold", pad=6)
    fig.savefig(path)
    plt.close(fig)


def fig_csv_panel() -> None:
    """Muestra del panel largo ordenado: filas que ilustran los tres regímenes C/F/U."""
    d = pd.read_csv(ROOT / "data" / "processed" / "visa_panel_long.csv")
    sub = d[(d.country == "mexico") & (d.category == "F2A") & (d.table == "FAD")].copy()
    rows = pd.concat([sub[sub.status == "C"].head(2), sub[sub.status == "F"].head(4)])
    extra_u = d[d.status == "U"].head(1)
    extra = d[(d.country == "india") & (d.category == "F1") & (d.table == "FAD") & (d.status == "F")].head(2)
    show = pd.concat([rows, extra, extra_u]).copy()
    # Es una MUESTRA de exhibición: cada serie en orden cronológico (las filas no cambian,
    # solo el orden en que se muestran), y las series en el orden en que entran a la muestra.
    serie = pd.Categorical(
        show["country"] + "/" + show["category"], categories=pd.unique(show["country"] + "/" + show["category"])
    )
    show = show.assign(_serie=serie).sort_values(["_serie", "bulletin_date"], kind="stable")
    show["country"] = show["country"].map(lambda c: COUNTRY_NAME.get(c, c))
    show["category"] = show["category"].map(cat_label)
    show = show[
        ["country", "category", "table", "bulletin_date", "status", "priority_date", "days_since_base", "raw_value"]
    ]
    # encabezados en español salvo la variable objetivo, que se muestra con su nombre real
    show.columns = [
        "país",
        "categoría",
        "tabla",
        "mes (t)",
        "estado",
        "fecha prioridad",
        "days_since_base",
        "celda cruda",
    ]
    for c in ("mes (t)", "fecha prioridad"):
        show[c] = show[c].astype(str).str.slice(0, 10).replace("nan", "")
    show["days_since_base"] = show["days_since_base"].apply(
        lambda x: "" if pd.isna(x) or x == "" else str(int(float(x)))
    )
    _render_df(
        show,
        FIG / "data_csv_panel.pdf",
        "Panel largo ordenado: visa_panel_long.csv (variable objetivo days_since_base)",
        width=6.0,
        fontsize=8.0,
        mono_cols=("days_since_base",),
        row_in=0.28,
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
