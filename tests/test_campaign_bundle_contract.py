"""Contrato end-to-end del bundle/CURRENT (P0R.5 · Incremento 2) — el mismo que corre el job CI
`campaign-bundle-contract`: merge #1 (sin previo) → merge #2 (EXCHANGE) → validate-current → NEGATIVO."""

from __future__ import annotations

import pytest

import tools.campaign_bundle_contract_smoke as smoke


def test_bundle_current_contract_end_to_end():
    ok, report = smoke.run_contract()
    assert ok, f"el contrato bundle/CURRENT falló: {report}"
    steps = {s["step"]: s for s in report["steps"]}
    assert steps["merge1_no_prev"]["rc"] == 0 and steps["merge1_no_prev"]["ok"]
    # merge #2 hace EXCHANGE: bundle distinto, previous = el bundle #1
    assert steps["merge2_exchange"]["prev"] == steps["merge1_no_prev"]["bundle_id"]
    assert steps["merge2_exchange"]["bundle_id"] != steps["merge1_no_prev"]["bundle_id"]
    # NEGATIVO obligatorio: puntero corrupto → validate-current rechaza
    assert steps["negative_corrupt_pointer"]["rc"] != 0 and steps["negative_corrupt_pointer"]["ok"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
