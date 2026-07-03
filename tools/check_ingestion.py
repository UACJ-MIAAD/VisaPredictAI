"""Gate de ingesta del cron semanal (A2/A3): el mes congelado DEBE entrar al panel.

Dos modos, ambos comparando el mes más nuevo en data/snapshots/ contra el mes más
nuevo del panel commiteado (data/processed/visa_panel_long.csv):

  --mode pending : imprime "stale" si hay un snapshot congelado que el panel aún no
                   ingiere (el Action reconstruye aunque new=0 — auto-reparación tras
                   un fallo a mitad de corrida), o "fresh" si no hay nada pendiente.
  --mode assert  : sale con código 1 si tras reconstruir el panel el mes congelado
                   sigue sin aparecer (deriva de formato → el parser lo dejó en 0
                   filas). Sin esto el job terminaba VERDE con "nothing to commit"
                   y el boletín jamás entraba.

Uso:  python tools/check_ingestion.py --mode {pending,assert}
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SNAP_DIR = ROOT / "data" / "snapshots"
PANEL = ROOT / "data" / "processed" / "visa_panel_long.csv"

# K3: presencia del mes-unión no basta — si la fuente cambia el markup de UNA
# sección (solo family, o solo la tabla DFF), ese bloque parsea a 0 filas sin
# excepción y el mes entra al panel por el otro bloque: medio boletín comiteado
# con todo verde. El mes recién ingerido debe traer las 4 combinaciones y un
# piso de filas (un boletín moderno trae ~120 entre las cuatro).
REQUIRED_COMBOS = {
    ("employment", "FAD"),
    ("employment", "DFF"),
    ("family", "FAD"),
    ("family", "DFF"),
}
MIN_ROWS_NEW_MONTH = 90


def month_coverage_problems(panel: pd.DataFrame) -> list[str]:
    """Return the coverage defects of the panel's newest month ([] = complete).

    Pure on a DataFrame with (block, table, bulletin_date) so the gate is unit-
    testable with synthetic panels.
    """
    per = pd.to_datetime(panel["bulletin_date"]).dt.to_period("M")
    newest = panel[per == per.max()]
    problems: list[str] = []
    combos = {(b, t) for b, t in newest[["block", "table"]].drop_duplicates().itertuples(index=False)}
    missing = REQUIRED_COMBOS - combos
    if missing:
        problems.append(f"combinaciones bloque×tabla ausentes: {sorted(missing)}")
    if len(newest) < MIN_ROWS_NEW_MONTH:
        problems.append(f"solo {len(newest)} filas (< {MIN_ROWS_NEW_MONTH})")
    return problems


def snapshot_max() -> pd.Timestamp:
    sys.path.insert(0, str(ROOT))
    from visa_common import extract_datetime_from_link

    months = [extract_datetime_from_link(p.name) for p in SNAP_DIR.glob("*.html")]
    dated = [m for m in months if m is not None]
    if not dated:
        raise SystemExit(f"ERROR: sin snapshots fechables en {SNAP_DIR}")
    return pd.Timestamp(max(dated))


def panel_max() -> pd.Timestamp:
    col = pd.read_csv(PANEL, usecols=["bulletin_date"])["bulletin_date"]
    return pd.to_datetime(col).max()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=("pending", "assert"), required=True)
    mode = ap.parse_args().mode
    s, p = snapshot_max(), panel_max()
    ingested = p.to_period("M") >= s.to_period("M")
    if mode == "pending":
        print("fresh" if ingested else "stale")
        return 0
    if not ingested:
        print(
            f"ERROR: snapshot {s:%Y-%m} congelado pero el panel termina en {p:%Y-%m} — "
            "el parser no ingirió el boletín nuevo (¿deriva de formato?)",
            file=sys.stderr,
        )
        return 1
    # K3: el mes está, pero ¿está COMPLETO? Deriva parcial de sección = medio
    # boletín con job verde; es el único fallo silencioso que quedaba.
    problems = month_coverage_problems(pd.read_csv(PANEL, usecols=["block", "table", "bulletin_date"]))
    if problems:
        print(
            f"ERROR: el mes más nuevo del panel ({p:%Y-%m}) está incompleto — "
            f"{'; '.join(problems)} (¿deriva de markup en una sección?)",
            file=sys.stderr,
        )
        return 1
    print(f"OK: panel al día ({p:%Y-%m} >= snapshot {s:%Y-%m}) y mes completo (4/4 bloque×tabla)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
