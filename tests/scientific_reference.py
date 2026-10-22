"""Fixed data for software regression, separate from the moving monthly panel.

The published September reference is NOT a new scientific validation or a claim
about current-vintage performance. Baseline values and tolerances stay unchanged.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from tests.published_inputs import _blob

ROOT = Path(__file__).resolve().parent.parent
REVISION = "3a9d94a9b547b8df58992be42a7462c45109c96b"
PANEL = "data/processed/visa_panel_long.csv"
SHA256 = "3ae64f3dd714668b7726ae75c95620f2e5542faa3e843e76123581610f00a0b8"


def reference_panel() -> bytes:
    raw = _blob(ROOT, REVISION, PANEL)
    assert hashlib.sha256(raw).hexdigest() == SHA256, "scientific reference input changed"
    return raw


def build_reference_warehouse(root: Path) -> tuple[Path, Path]:
    import duckdb
    import pandas as pd

    from pipeline.build_database import build

    raw = reference_panel()
    panel = root / "panel.csv"
    panel.write_bytes(raw)
    db = root / "reference.duckdb"
    with duckdb.connect(str(db)) as con:
        # Only priority-date series are consumed. No DV/source-lineage claims
        # are made for this isolated test warehouse; production never uses it.
        build(con, pd.read_csv(io.BytesIO(raw)), allow_degraded=True)
    return db, panel


@pytest.fixture(scope="session")
def reference_warehouse(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    return build_reference_warehouse(tmp_path_factory.mktemp("scientific-reference"))


@pytest.fixture
def fixed_scientific_data(reference_warehouse: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from vp_model import dataset

    db, panel = reference_warehouse
    monkeypatch.setattr(dataset, "DB_PATH", db)
    monkeypatch.setattr(dataset, "PANEL_CSV", panel)
