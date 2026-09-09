"""E1 · Genera el catálogo de cohortes de estabilidad (RULE v1.0.0), determinista.

Salidas: ``reports/eval/series_cohorts.json`` y ``.csv``, con la misma información en los
dos formatos (una prueba lo exige). Todo lo que describe una serie se calcula **antes** de
su ``holdout_start``; el hold-out no entra ni en las features ni en la regla ni en la
elegibilidad, y una prueba metamórfica lo comprueba mutándolo entero.

E1 es **exploratorio**: la regla se registra ANTES de mirar resultados de modelado y no se
ajusta por ellos. Sólo E3/E4 —o datos posteriores a este registro— pueden leerse como
confirmación.

Uso:  ante/bin/python experiments/build_cohorts.py [--panel RUTA] [--out-dir RUTA]
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from vp_model import stability
from vp_model.config import HOLDOUT, SEASONAL_PERIOD
from vp_model.dataset import is_evaluable

ROOT = Path(__file__).resolve().parent.parent
PANEL = ROOT / "data" / "processed" / "visa_panel_long.parquet"
OUT_DIR = ROOT / "reports" / "eval"
CLAVE = ("country", "block", "category", "table")
DECIMALES = 6

COLUMNAS = [
    *CLAVE,
    "cohort",
    "congelada",
    "retro_reciente",
    *stability.FEATURE_NAMES,
    "n_pre",
    "n_F",
    "span_months",
    "evaluable",
    "holdout_start",
]


def _ruta_legible(ruta: Path) -> str:
    """Relativa al repositorio cuando vive dentro; si no, su nombre (panel de prueba)."""
    try:
        return str(ruta.resolve().relative_to(ROOT))
    except ValueError:
        return ruta.name


def _raw_f(g: pd.DataFrame) -> pd.Series:
    """Serie F cruda de un grupo del panel: días desde la época, indexada por boletín."""
    f = g[g.status == "F"].sort_values("bulletin_date")
    return pd.Series(
        f.days_since_base.astype("float64").to_numpy(),
        index=pd.DatetimeIndex(f.bulletin_date),
        name="days_since_base",
    )


def _poblacion(panel: pd.DataFrame) -> pd.DataFrame:
    """Censo de series estructurales con su elegibilidad CANÓNICA (``is_evaluable``)."""
    filas = []
    for clave, g in panel.groupby(list(CLAVE), sort=True):
        raw = _raw_f(g)
        if len(raw):
            meses = raw.index.to_period("M")
            span = int((meses.max() - meses.min()).n) + 1
        else:
            span = 0
        filas.append(
            {
                **dict(zip(CLAVE, clave, strict=True)),
                "n_F": len(raw),
                "span_months": span,
                "evaluable": bool(len(raw) and is_evaluable(len(raw), span, clave[3])),
            }
        )
    return pd.DataFrame(filas).sort_values(list(CLAVE)).reset_index(drop=True)


def _leer_panel(ruta: Path) -> pd.DataFrame:
    """Lee el panel por su extensión.

    El panel vive en el repositorio en dos serializaciones gobernadas: ``.parquet`` (la que
    consume el modelado, out de DVC) y ``.csv`` (versionada). Aceptar las dos evita exigir
    un motor de parquet donde no hace falta: el job base de CI instala solo ``.[dev]``, sin
    pyarrow.
    """
    if ruta.suffix == ".parquet":
        panel = pd.read_parquet(ruta)
    elif ruta.suffix == ".csv":
        panel = pd.read_csv(ruta)
    else:
        raise ValueError(f"panel no reconocido: {ruta.name} (se espera .parquet o .csv)")
    panel["bulletin_date"] = pd.to_datetime(panel["bulletin_date"])
    return panel


def build(panel_path: Path = PANEL) -> dict:
    """Catálogo completo: provenance + población + una fila por serie elegible."""
    panel = _leer_panel(panel_path)
    censo = _poblacion(panel)
    grupos = dict(list(panel.groupby(list(CLAVE), sort=True)))

    filas = []
    for r in censo[censo.evaluable].itertuples(index=False):
        clave = (r.country, r.block, r.category, r.table)
        raw = _raw_f(grupos[clave])
        corte = stability.holdout_start(raw)
        f = stability.pre_split_features(raw, corte)
        filas.append(
            {
                **dict(zip(CLAVE, clave, strict=True)),
                "cohort": stability.classify(f),
                **{k: bool(v) for k, v in stability.annotate(f).items()},
                **{k: (v if isinstance(v, int) else round(float(v), DECIMALES)) for k, v in f.as_dict().items()},
                "n_F": int(r.n_F),
                "span_months": int(r.span_months),
                "evaluable": True,
                "holdout_start": corte.strftime("%Y-%m-%d"),
            }
        )
    series = sorted(filas, key=lambda d: tuple(d[c] for c in CLAVE))

    reparto: dict[str, int] = {}
    for fila in series:
        reparto[fila["cohort"]] = reparto.get(fila["cohort"], 0) + 1

    return {
        "rule_version": stability.RULE_VERSION,
        "rule": {
            "no_estable_si": f"retro_rate_pre > {stability.RETRO_RATE_MAX} o "
            f"worst_retro_scaled_pre > {stability.WORST_RETRO_SCALED_MAX}",
            "retro_rate_max": stability.RETRO_RATE_MAX,
            "worst_retro_scaled_max": stability.WORST_RETRO_SCALED_MAX,
            "recent_window_months": stability.RECENT_WINDOW_MONTHS,
            "annotations": ["congelada", "retro_reciente"],
            "status": "exploratoria",
            "registered_before": "cualquier resultado de modelado por cohorte (E2 en adelante)",
        },
        "split": {
            "holdout_months": HOLDOUT,
            "seasonal_period": SEASONAL_PERIOD,
            "holdout_start_rule": "to_regular_monthly_causal(raw).index[-HOLDOUT]",
            "features_window": "observaciones F estrictamente anteriores a holdout_start",
        },
        "population": {
            "n_structural": int(len(censo)),
            "n_with_F": int((censo.n_F > 0).sum()),
            "n_evaluable": int(censo.evaluable.sum()),
            "eligibility": "vp_model.dataset.is_evaluable (definición única N1)",
        },
        "cohorts": dict(sorted(reparto.items())),
        "provenance": {
            "panel": _ruta_legible(panel_path),
            "panel_sha256": hashlib.sha256(panel_path.read_bytes()).hexdigest(),
            "panel_rows": int(len(panel)),
            "panel_months": int(panel.bulletin_date.dt.to_period("M").nunique()),
            "panel_last_month": panel.bulletin_date.max().strftime("%Y-%m"),
            "features": list(stability.FEATURE_NAMES),
        },
        "series": series,
    }


def write(catalogo: dict, out_dir: Path = OUT_DIR) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    ruta_json = out_dir / "series_cohorts.json"
    ruta_csv = out_dir / "series_cohorts.csv"
    ruta_json.write_text(json.dumps(catalogo, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    pd.DataFrame(catalogo["series"], columns=COLUMNAS).to_csv(ruta_csv, index=False, lineterminator="\n")
    return ruta_json, ruta_csv


def diagnostics(catalogo: dict) -> dict:
    """Sensibilidad 3x3 y agrupamiento no supervisado, SOLO como diagnóstico.

    No toca los umbrales registrados ni elige la regla por el resultado: sirve para saber
    cuánto de la partición es un artefacto del corte y cuánto la separa cualquier método.
    ``KMeans``/``Ward`` se importan aquí dentro porque el catálogo no depende de ellos.
    """
    import numpy as np
    from scipy.cluster.hierarchy import fcluster, linkage
    from sklearn.cluster import KMeans

    d = pd.DataFrame(catalogo["series"])
    rejilla = []
    for rr in (0.01, stability.RETRO_RATE_MAX, 0.04):
        for wr in (3.0, stability.WORST_RETRO_SCALED_MAX, 8.0):
            inestable = (d.retro_rate_pre > rr) | (d.worst_retro_scaled_pre > wr)
            igual = (~inestable) == (d.cohort == "estable")
            rejilla.append(
                {
                    "retro_rate_max": rr,
                    "worst_retro_scaled_max": wr,
                    "n_estable": int((~inestable).sum()),
                    "n_no_estable": int(inestable.sum()),
                    "acuerdo_con_v1": round(float(igual.mean()), 4),
                }
            )

    X = d[list(stability.FEATURE_NAMES)].to_numpy(dtype="float64")
    X = np.nan_to_num(X, nan=0.0)
    X = (X - X.mean(axis=0)) / np.where(X.std(axis=0) == 0, 1.0, X.std(axis=0))
    etiquetas = {
        "kmeans": KMeans(n_clusters=2, n_init=10, random_state=0).fit_predict(X),
        "ward": fcluster(linkage(X, method="ward"), t=2, criterion="maxclust") - 1,
    }
    estable = (d.cohort == "estable").to_numpy()
    acuerdos = {}
    for nombre, lab in etiquetas.items():
        directo = float((lab.astype(bool) == estable).mean())
        acuerdos[nombre] = {
            "acuerdo_max": round(max(directo, 1 - directo), 4),
            "tam_grupos": sorted(int((lab == k).sum()) for k in set(lab.tolist())),
        }
    return {"sensibilidad_3x3": rejilla, "agrupamiento": acuerdos}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--panel", type=Path, default=PANEL)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--diagnostics", action="store_true", help="sensibilidad 3x3 + KMeans/Ward")
    args = ap.parse_args()
    catalogo = build(args.panel)
    rutas = write(catalogo, args.out_dir)
    print(
        f"RULE v{catalogo['rule_version']} · {catalogo['population']['n_evaluable']} elegibles de "
        f"{catalogo['population']['n_structural']} ({catalogo['population']['n_with_F']} con F) · "
        f"{catalogo['cohorts']} · corte por serie"
    )
    for r in rutas:
        print(f"  → {_ruta_legible(r)}")
    if args.diagnostics:
        print(json.dumps(diagnostics(catalogo), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
