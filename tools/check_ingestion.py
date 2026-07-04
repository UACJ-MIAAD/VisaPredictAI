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
    print(f"OK: panel al día ({p:%Y-%m} >= snapshot {s:%Y-%m})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
