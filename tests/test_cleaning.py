"""Tests de la política de limpieza central (plan FE/cleaning, épicas AA/AB).

Cubre: el registro CLEANING_DECISIONS está bien formado, el ledger por build es
fresco y consistente con el panel, los guards de dominio/fechas de build_panel
abortan (AA3/AA4), y la caracterización EDA ya no fabrica rampas (AB1).

Runs with pytest *or* as a plain script (no pytest required):
    ante/bin/python tests/test_cleaning.py
"""

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vp_data.cleaning import CLEANING_DECISIONS, LEDGER_PATH  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PANEL = ROOT / "data" / "processed" / "visa_panel_long.csv"


def test_decisions_registry_wellformed():
    ids = [d["id"] for d in CLEANING_DECISIONS]
    assert len(ids) == len(set(ids)), "ids duplicados en CLEANING_DECISIONS"
    for d in CLEANING_DECISIONS:
        assert set(d) == {"id", "title", "module", "rationale"}, f"campos inesperados en {d.get('id')}"
        assert d["rationale"].strip(), f"rationale vacío en {d['id']}"
    # Los módulos citados existen (el registro no puede apuntar a archivos muertos).
    for d in CLEANING_DECISIONS:
        for ref in d["module"].split("·"):
            rel = ref.strip().split(":")[0]
            assert (ROOT / rel).exists(), f"{d['id']} cita módulo inexistente: {rel}"


def test_ledger_fresh_and_consistent():
    ledger_fp = ROOT / LEDGER_PATH
    assert ledger_fp.exists(), "cleaning_ledger.json no existe — correr python -m pipeline.build_panel"
    ledger = json.loads(ledger_fp.read_text())
    panel = pd.read_csv(PANEL, parse_dates=["bulletin_date"])
    assert ledger["vintage"] == panel.bulletin_date.max().strftime("%Y-%m"), "ledger desfasado vs panel"
    assert ledger["n_rows"] == len(panel)
    assert ledger["rows_by_status"]["F"] == int((panel.status == "F").sum())
    # Guards documentadas en cero: un valor >0 es inalcanzable (build_panel aborta).
    for guard in ("bulletin_date_unparseable", "f_priority_date_unparseable", "epoch_underflow"):
        assert ledger[guard] == 0, f"{guard} debe ser 0 por construcción"
    assert ledger["dup_collapsed"] >= 0
    assert ledger["big_jumps_gt_8y"] >= 0


def _family_csv(tmp: Path, f_level: str, month: str = "2026-07-01") -> None:
    pd.DataFrame(
        {
            "F_level": [f_level],
            "priority_date": ["2010-05-01"],
            "visa_bulletin_date": [month],
            "raw_value": ["01MAY10"],
            "status": ["F"],
            "table_type": ["final_action"],
            "visa_wait_time": [16.2],
        }
    ).to_csv(tmp / "mexico_family_visa_backlog_timecourse.csv", index=False)


def test_family_domain_guard_aborts(tmp_path=None):
    import tempfile

    from pipeline import build_panel as bp

    tmp = Path(tempfile.mkdtemp()) if tmp_path is None else tmp_path
    _family_csv(tmp, f_level="9")  # fuera de dominio {1,2A,2B,3,4}
    old_raw, old_countries = bp.RAW, bp.COUNTRIES
    bp.RAW, bp.COUNTRIES = tmp, {"mexico": "mexico"}
    try:
        try:
            bp.load_family()
            raise AssertionError("F_level fuera de dominio debió abortar (AA4)")
        except SystemExit as e:
            assert "F_level" in str(e)
    finally:
        bp.RAW, bp.COUNTRIES = old_raw, old_countries


def test_eda_clean_no_nan_preserves_observed_no_linear_ramp():
    # AB1: _clean produce serie completa, respeta lo observado y los huecos largos
    # ya NO son la rampa lineal (Kalman de espacio de estados, no interpolate()).
    from vp_model import dataset, features, preprocess

    raw = dataset.load_series("china", "F1", "FAD")  # serie con huecos conocidos
    s = features._clean("china", "F1", "FAD")
    assert not s.isna().any(), "la caracterización exige serie completa"
    grid = preprocess.to_regular_monthly(raw)
    observed = raw.index.intersection(s.index)
    import numpy as np

    assert np.allclose(s.loc[observed].to_numpy(), raw.loc[observed].to_numpy(), rtol=1e-9)
    long_gaps = grid.isna()
    if long_gaps.any():
        # En al menos un hueco largo, el valor imputado difiere de la rampa lineal
        # (si coincidieran todos, seguiríamos interpolando linealmente sin tope).
        linear = grid.interpolate(limit_direction="both")
        assert not np.allclose(s[long_gaps].to_numpy(), linear[long_gaps].to_numpy()), (
            "los huecos largos siguen siendo la rampa lineal — AB1 no aplicado"
        )


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
