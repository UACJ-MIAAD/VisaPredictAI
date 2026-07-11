"""E2/#27: las tablas de caracterización del deliverable se DERIVAN, no se tipean.

Anti-drift (patrón KEYFACTS): cada fila de tab:features_estructura y
tab:features_anomalias del .tex debe coincidir EXACTAMENTE con la regeneración desde
vp_model.series_characterization — fueron hand-built y aportaron 2 de los 6 errores
numéricos del audit ciego del 7-jul. Si el panel cambia, este test obliga a regenerar
las filas con make_tex_tables (y el diff queda visible en el PR).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("statsmodels")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments"))

from vp_model import dataset  # noqa: E402

pytestmark = pytest.mark.skipif(not dataset.DB_PATH.exists(), reason="almacén DuckDB ausente")

import make_tex_tables as mtt  # noqa: E402

TEX = (ROOT / "reports" / "latex" / "ProyectoI_VisaPredictAI.tex").read_text()


def _data_rows(block: str) -> list[str]:
    return [r for r in block.splitlines() if not r.lstrip().startswith("%")]


def test_features_estructura_matches_derivation() -> None:
    rows = _data_rows(mtt.features_estructura_rows())
    assert len(rows) == 25
    missing = [r for r in rows if r not in TEX]
    assert not missing, "filas de estructura desalineadas con la derivación:\n" + "\n".join(missing[:5])


def test_features_anomalias_matches_derivation() -> None:
    rows = _data_rows(mtt.features_anomalias_rows())
    assert len(rows) == 25
    missing = [r for r in rows if r not in TEX]
    assert not missing, "filas de anomalías desalineadas con la derivación:\n" + "\n".join(missing[:5])


def test_generated_markers_present() -> None:
    """Las tablas declaran su generador — nadie vuelve a tipearlas a mano sin verse."""
    assert "tab:features_estructura: vp_model.series_characterization" in TEX
    assert "tab:features_anomalias: vp_model.series_characterization" in TEX
