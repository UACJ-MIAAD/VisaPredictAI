"""Corre el gate de promoción prospectivo pre-registrado (A4) y emite la decisión.

Lee los scorecards del campeón y del sombra (generados por ``score_forecasts.py``),
construye los pares del mismo universo (``score_forecasts._pairs``) y aplica la política
pre-registrada ``vp_model.promotion.POLICY`` → escribe
``reports/governance/promotion_decision.json`` con la decisión por tabla
(``promote`` · ``retain`` · ``extend-shadow`` · ``reject``), sus razones y la política
íntegra (auditable). La muestra insuficiente NUNCA produce ``promote``.

En el cron corre tras ``score_forecasts.py``; la promoción real sigue siendo humana
(``run_champion_challenger.py --promote``, que se rehúsa sin decisión ``promote``).

Corre en ``ante`` desde la raíz:  ante/bin/python experiments/run_promotion_gate.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import score_forecasts as sf  # noqa: E402

from vp_model import config, promotion  # noqa: E402

log = config.get_logger("promotion_gate")


def _load(path: Path) -> pd.DataFrame:
    """Scorecard o frame vacío (un sombra sin filas puntuadas escribe un CSV sin columnas)."""
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def main() -> int:
    prosp = ROOT / "reports" / "prospective"
    champ = _load(prosp / "forecast_scorecard.csv")
    shadow = _load(prosp / "forecast_scorecard_shadow.csv")
    pairs = sf._pairs(champ, shadow)
    decision = promotion.decide(pairs)
    out = ROOT / "reports" / "governance" / "promotion_decision.json"
    out.write_text(json.dumps(decision, ensure_ascii=False, indent=2) + "\n")
    if decision["by_table"]:
        for table, entry in decision["by_table"].items():
            log.info("[%s] decisión = %s · %s", table, entry["decision"], "; ".join(entry["reasons"]))
    else:
        log.info("sin pares campeón-sombra aún — decisión global: seguir acumulando sombra")
    log.info("decisión → %s (política v%s)", out, promotion.POLICY["policy_version"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
