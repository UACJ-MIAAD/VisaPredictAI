"""M74-F · deuda 8 — ¿por qué cambian ETS/Theta/ARIMA sobre las MISMAS fechas entre dos añadas?

Diagnóstico de sólo lectura: no ajusta modelos ni escribe en ``reports/``. Compara dos
``holdout_forecasts_{tabla}.csv`` (la base, por ruta o por ``git:<ref>:<ruta>``, y la nueva) sobre
las claves que comparten (modelo, país, categoría, fecha) y cruza la coincidencia, serie a serie,
con la estructura de huecos del panel.

Contexto medido (recibo `M74E_CAMPANA_COMPLETA_953cd00_RECEIPT.md` §5.1): en FAD, naive1 coincide
550/550 en el solape y ETS 352/550, Theta 106/550, ARIMA 25/528. La base de HEAD (`854e404`,
7-jul-2026) es ANTERIOR a `9f58ba6` (12-jul-2026, política de huecos causal LOCF, FE 2.0.0). Si esa
es la causa, la firma es: en series SIN huecos interiores antes del hold-out los pronósticos
coinciden y en series CON huecos difieren (el relleno de la serie de entrenamiento cambia), mientras
naive1 —que sólo mira la última observación— coincide en todas.

El script no decide la causa: reporta la tabla de contingencia por modelo × (con/sin huecos) y la
procedencia de la base para que la conclusión se escriba sobre números.

Uso (desde la raíz):
    python experiments/diagnose_holdout_drift.py --table FAD \\
        --base git:HEAD:reports/eval/holdout_forecasts_FAD.csv \\
        --new  ../VisaPredictAI-m74e-exec/reports/eval/holdout_forecasts_FAD.csv \\
        --panel ../VisaPredictAI-m74e-exec/data/processed/visa_panel_long.csv \\
        --out  /ruta/fuera/de/reports/drift_FAD.json
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
KEY = ["model", "country", "category", "date"]
#: Tolerancia de igualdad numérica: los CSV redondean distinto según la añada del escritor.
ATOL = 1e-6


def leer_csv(spec: str, *, root: Path = ROOT) -> tuple[pd.DataFrame, dict]:
    """Lee una ruta o ``git:<ref>:<ruta>`` y devuelve el marco y su procedencia."""
    if spec.startswith("git:"):
        _, ref, rel = spec.split(":", 2)
        texto = subprocess.run(
            ["git", "show", f"{ref}:{rel}"], cwd=root, capture_output=True, text=True, check=True
        ).stdout
        log = subprocess.run(
            ["git", "log", "-1", "--format=%H|%cI|%s", ref, "--", rel], cwd=root, capture_output=True, text=True
        ).stdout.strip()
        sha, fecha, asunto = (log.split("|", 2) + ["", "", ""])[:3]
        con_recibo = (
            subprocess.run(
                ["git", "cat-file", "-e", f"{ref}:{rel}.receipt.json"], cwd=root, capture_output=True
            ).returncode
            == 0
        )
        proc = {"source": spec, "last_commit": sha, "commit_date": fecha, "subject": asunto, "has_receipt": con_recibo}
        return pd.read_csv(io.StringIO(texto)), proc
    ruta = Path(spec)
    recibo = ruta.with_suffix(ruta.suffix + ".receipt.json")
    proc = {"source": str(ruta), "has_receipt": recibo.is_file()}
    if recibo.is_file():
        acta = json.loads(recibo.read_text(encoding="utf-8"))
        proc.update({k: acta.get(k) for k in ("campaign_id", "code_sha", "panel_sha256")})
    return pd.read_csv(ruta), proc


def huecos_interiores(panel: pd.DataFrame, table: str, antes_de: pd.Timestamp) -> dict[tuple[str, str], int]:
    """Meses SIN observación F entre la primera F y ``antes_de`` (exclusivo), por (país, categoría).

    Es exactamente lo que una política de relleno (interpolación frente a LOCF) trata distinto: sin
    huecos interiores, la serie de entrenamiento es la misma bajo cualquiera de las dos.
    """
    f = panel[(panel["table"] == table) & (panel["status"] == "F")].copy()
    f["mes"] = pd.to_datetime(f["bulletin_date"]).dt.to_period("M")
    corte = antes_de.to_period("M")
    out: dict[tuple[str, str], int] = {}
    for (pais, cat), g in f.groupby(["country", "category"]):
        meses = set(g.loc[g["mes"] < corte, "mes"])
        if not meses:
            continue
        rango = pd.period_range(min(meses), corte - 1, freq="M")
        out[(pais, cat)] = int(sum(m not in meses for m in rango))
    return out


def comparar(base: pd.DataFrame, nuevo: pd.DataFrame) -> pd.DataFrame:
    """Claves compartidas con la marca de igualdad del pronóstico (y de la observación real)."""
    for d in (base, nuevo):
        d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
    m = base.merge(nuevo, on=KEY, suffixes=("_base", "_new"))
    m["same_forecast"] = (m["forecast_base"] - m["forecast_new"]).abs() <= ATOL
    m["same_actual"] = (m["actual_base"] - m["actual_new"]).abs() <= ATOL
    return m


def contingencia(m: pd.DataFrame, gaps: dict[tuple[str, str], int]) -> list[dict]:
    m = m.copy()
    m["has_gaps"] = [gaps.get((p, c), -1) > 0 for p, c in zip(m["country"], m["category"], strict=True)]
    filas = []
    for (modelo, con), g in m.groupby(["model", "has_gaps"]):
        series = g.groupby(["country", "category"])["same_forecast"].all()
        filas.append(
            {
                "model": modelo,
                "has_interior_gaps": bool(con),
                "n_rows": int(len(g)),
                "rows_identical": int(g["same_forecast"].sum()),
                "n_series": int(len(series)),
                "series_all_identical": int(series.sum()),
            }
        )
    return filas


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--table", required=True, choices=["FAD", "DFF"])
    ap.add_argument("--base", required=True, help="ruta o git:<ref>:<ruta>")
    ap.add_argument("--new", required=True)
    ap.add_argument("--panel", required=True)
    ap.add_argument("--out", type=Path, required=True)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if (ROOT / "reports") in args.out.resolve().parents:
        raise SystemExit("--out apunta dentro de reports/: esto es diagnóstico, no un artefacto de la campaña")
    base, proc_base = leer_csv(args.base)
    nuevo, proc_new = leer_csv(args.new)
    m = comparar(base, nuevo)
    inicio = pd.Timestamp(min(base["date"].min(), nuevo["date"].min()))
    gaps = huecos_interiores(pd.read_csv(args.panel), args.table, inicio)
    informe = {
        "schema": "holdout-drift-diagnosis/1",
        "table": args.table,
        "base": proc_base,
        "new": proc_new,
        "window_base": [str(base["date"].min()), str(base["date"].max())],
        "window_new": [str(nuevo["date"].min()), str(nuevo["date"].max())],
        "gaps_measured_before": str(inicio.date()),
        "actuals_identical_on_overlap": bool(m["same_actual"].all()),
        "by_model": {
            mod: {"shared": int(len(g)), "identical": int(g["same_forecast"].sum())} for mod, g in m.groupby("model")
        },
        "contingency": contingencia(m, gaps),
        "series_gaps": {f"{p}/{c}": n for (p, c), n in sorted(gaps.items())},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(informe, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for fila in informe["contingency"]:
        print(
            f"{fila['model']:<9} huecos={'sí' if fila['has_interior_gaps'] else 'no'}  "
            f"filas {fila['rows_identical']}/{fila['n_rows']} idénticas · "
            f"series {fila['series_all_identical']}/{fila['n_series']} idénticas"
        )
    print(f"→ {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
