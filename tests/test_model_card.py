"""La MODEL_CARD.md no debe quedar obsoleta respecto a key_facts.json (la fuente de verdad).

Corre en el job base (sin darts): solo lee JSON + el markdown committeado. Si alguien
regenera key_facts y olvida `make model-card`, esto falla — la misma disciplina del
guardián de consistencia, aplicada a la tarjeta de modelo.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CARD = ROOT / "reports" / "MODEL_CARD.md"
KEY_FACTS = ROOT / "reports" / "key_facts.json"


def test_model_card_in_sync_with_key_facts() -> None:
    assert CARD.exists(), "falta reports/MODEL_CARD.md; genérala con `make model-card`"
    card = CARD.read_text()
    kf = json.loads(KEY_FACTS.read_text())

    expected = [
        f"{kf['n_obs']:,}",  # observaciones del panel
        f"{kf['n_obs_F']:,}",  # entrenables F
        str(kf["n_series_evaluable"]),  # series evaluables
        str(kf["prosp_mae_days"]),  # MAE prospectivo
        str(kf["prosp_mase"]),  # MASE prospectivo
        kf["date_last"],  # último mes
    ]
    missing = [v for v in expected if v not in card]
    assert not missing, (
        f"MODEL_CARD.md desalineada con key_facts.json (faltan {missing}); regenérala con `make model-card`"
    )
