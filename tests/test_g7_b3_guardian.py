"""G7 B3: el guardián ejecutado sobre texto causal y mutaciones aisladas de sus afirmaciones."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_consistency as cc  # noqa: E402

RULES = yaml.safe_load((ROOT / "tools/consistency_rules.yml").read_text())


def _guard(tmp_path: Path, monkeypatch, text: str, *, decimal: bool = False) -> int:
    paper = tmp_path / "claim.tex"
    paper.write_text(text + "\n")
    facts = tmp_path / "reports/governance/key_facts.json"
    facts.parent.mkdir(parents=True)
    facts.write_text(json.dumps({"ets_fad_mean": 0.167}))
    selected = [r for r in RULES["decimal"] if r["fact"] == "ets_fad_mean" and r["in"] == ["paper"]] if decimal else []
    config = {
        "artifacts": {"paper": [str(paper)]},
        "forbidden": [r for r in RULES["forbidden"] if "MCS de la campaña causal" in r.get("reason", "")],
        "decimal": selected,
    }
    assert len(config["forbidden"]) == 1
    rules_path = tmp_path / "rules.yml"
    rules_path.write_text(yaml.safe_dump(config))
    monkeypatch.setattr(cc, "ROOT", tmp_path)
    monkeypatch.setattr(cc, "RULES_PATH", rules_path)
    return cc.main()


@pytest.mark.parametrize("value, rc", [("0.167", 0), ("0.168", 1)])
def test_ets_fad_is_checked_in_the_reconciled_sentence(tmp_path, monkeypatch, capsys, value, rc) -> None:
    text = f"scores 0.109). Among trained models, ETS and Theta lead (hold-out {value} and 0.171 on\nFAD; 0.129 and 0.125 on DFF)."
    assert _guard(tmp_path, monkeypatch, text, decimal=True) == rc
    output = capsys.readouterr().out
    if rc:
        assert "DECIMAL" in output and "ets_fad_mean" in output


@pytest.mark.parametrize(
    "text, rc",
    [
        ("El MCS retiene solo al naïve-1 en ambas tablas.", 1),
        ("MCS = {naive1} en ambas tablas.", 1),
        ("Naive1 is the only model that survives in both tables.", 1),
        ("El MCS retiene solo al naïve-1 en FAD; en DFF lo acompaña ETS.", 0),
        ("On FAD only naive-1 survives; on DFF it is joined by ETS.", 0),
        ("La MASE de selección de los árboles en FAD es 0.107.", 0),
    ],
)
def test_mcs_dead_claim_and_legitimate_causal_prose(tmp_path, monkeypatch, text, rc) -> None:
    assert _guard(tmp_path, monkeypatch, text) == rc
