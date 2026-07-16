"""Gate AST anti-mutación-cruda (P0R.5 · Incremento 1R3 · B179/B180) + sanidad del contrato CSV del bundle. Enforce
que `tools/campaign_bundle.py` NO llame primitivas destructivas crudas (`os.unlink`/`remove`/`rename`/`replace`/
`rmdir`) — deben pasar por la cuarentena gobernada de `tools/governed_fs.py`."""

from __future__ import annotations

import json
import os
import tempfile

import tools.check_raw_fs_mutations as gate


def test_bundle_layer_has_no_raw_destructive_mutations():
    assert gate.main() == 0


def test_gate_catches_a_raw_mutation():
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write("import os\ndef f(fd):\n    os.unlink('x', dir_fd=fd)\n")
        path = fh.name
    try:
        assert gate._violations(path), "el gate no detectó os.unlink crudo"
    finally:
        os.unlink(path)


def test_csv_contract_is_well_formed():
    root = os.path.dirname(os.path.dirname(os.path.abspath(gate.__file__)))
    contract = json.load(open(os.path.join(root, "security", "campaign_bundle_contract.json")))
    assert contract["encoding"] == "utf-8"
    assert isinstance(contract["columns"], list) and len(contract["columns"]) == 20
    assert contract["columns"][0] == "run_id" and contract["columns"][-1] == "source_run_id"
    assert set(contract["outputs"]) == {"campaign", "eval"}


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
