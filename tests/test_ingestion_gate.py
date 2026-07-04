"""Unit tests for the K3 month-completeness gate (tools/check_ingestion.py).

The A2 assert only compared the union max month, so partial section drift
(family parses to 0 rows, employment still lands the month) committed half a
bulletin with a green job. month_coverage_problems() must catch exactly that.

Runs with pytest *or* as a plain script (no pytest required):
    ante/bin/python tests/test_ingestion_gate.py
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.check_ingestion import (  # noqa: E402
    MIN_ROWS_NEW_MONTH,
    model_artifacts_stale,
    month_coverage_problems,
)


def _month(block_tables: list[tuple[str, str]], month: str, rows_per_combo: int) -> pd.DataFrame:
    return pd.DataFrame(
        [{"block": b, "table": t, "bulletin_date": month} for b, t in block_tables for _ in range(rows_per_combo)]
    )


FULL = [("employment", "FAD"), ("employment", "DFF"), ("family", "FAD"), ("family", "DFF")]


def test_complete_month_passes():
    panel = _month(FULL, "2026-07-01", 30)  # 120 filas, 4/4 combos
    assert month_coverage_problems(panel) == []


def test_missing_block_fails():
    # Deriva de markup en la sección family: el mes entra solo por employment.
    old = _month(FULL, "2026-06-01", 30)
    new = _month([("employment", "FAD"), ("employment", "DFF")], "2026-07-01", 30)
    problems = month_coverage_problems(pd.concat([old, new], ignore_index=True))
    assert problems, "un mes sin el bloque family debe fallar el gate"
    assert "family" in problems[0]


def test_missing_single_table_fails():
    # Solo la tabla DFF de empleo deja de matchear (etiquetado por ordinal).
    combos = [("employment", "FAD"), ("family", "FAD"), ("family", "DFF")]
    problems = month_coverage_problems(_month(combos, "2026-07-01", 40))
    assert any("DFF" in p for p in problems)


def test_row_floor_fails():
    # 4/4 combos pero casi vacíos: un parser que emite 2 filas por tabla no pasa.
    problems = month_coverage_problems(_month(FULL, "2026-07-01", 2))
    assert any(str(MIN_ROWS_NEW_MONTH) in p for p in problems)


def test_only_newest_month_is_judged():
    # Un mes histórico incompleto (pre-DFF 2015) NO debe disparar el gate.
    old = _month([("employment", "FAD"), ("family", "FAD")], "2014-01-01", 30)
    new = _month(FULL, "2026-07-01", 30)
    assert month_coverage_problems(pd.concat([old, new], ignore_index=True)) == []


# --- model_artifacts_stale (#17: auto-reparación del bloque de modelado) ---


def test_model_artifacts_fresh():
    assert model_artifacts_stale("2026-07", {"date_last": "2026-07"}, {"vintage": "2026-07"}) == []


def test_model_artifacts_lag_both():
    # Huella del fallo real: el mes entró al panel pero model/eda murieron después.
    lagging = model_artifacts_stale("2026-08", {"date_last": "2026-07"}, {"vintage": "2026-07"})
    assert len(lagging) == 2
    assert any("key_facts" in item for item in lagging)
    assert any("eda_facts" in item for item in lagging)


def test_model_artifacts_lag_one():
    # Solo el EDA (4g, continue-on-error) quedó atrás: también debe repararse.
    lagging = model_artifacts_stale("2026-08", {"date_last": "2026-08"}, {"vintage": "2026-07"})
    assert lagging == ["eda_facts vintage=2026-07"]


def test_model_artifacts_missing_keys_are_stale():
    # Un artefacto sin su campo de vintage cuenta como desfasado, no como fresco.
    lagging = model_artifacts_stale("2026-08", {}, {})
    assert len(lagging) == 2


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {fn.__name__}: {e}")
    print(f"\n{passed}/{passed + failed} casos OK" + (" ✓" if not failed else f"  ({failed} FALLAN)"))
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if _run() else 1)
