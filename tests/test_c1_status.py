"""C1: an old caveat cannot override the declared protocol; corrupt feeds cannot pass release checks."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments"))
import build_model_card as card  # noqa: E402

from tools import check_contracts  # noqa: E402
from vp_data import ingestion_state  # noqa: E402


@pytest.fixture
def card_tree(tmp_path, monkeypatch):
    reports = tmp_path / "reports"
    (reports / "governance").mkdir(parents=True)
    (tmp_path / "tools").mkdir()
    monkeypatch.setattr(card, "ROOT", tmp_path)
    monkeypatch.setattr(card, "REPORTS", reports)
    monkeypatch.setattr(card, "E5_FACTS", reports / "governance/e5_facts.json")
    monkeypatch.setattr(card.tracking, "git_state", lambda: ("a" * 12, False))
    monkeypatch.setattr(card.tracking, "pipeline_run_id", lambda: "test-c1")
    return tmp_path


def test_causal_without_evidence_does_not_claim_precausal_work_is_still_running(card_tree):
    (card_tree / "tools/consistency_rules.yml").write_text("retro_protocol: f2-causal\n")
    text = card.render("2026-09-test")
    assert "re-derivación en curso" not in text
    assert "f2-causal" in text
    assert "sin acreditación" in text


def test_unknown_protocol_does_not_invent_pre_f1(card_tree):
    text = card.render("2026-09-test")
    assert "protocolo previo a la" not in text
    assert "no verificado" in text


def test_pre_f1_keeps_its_warning(card_tree):
    (card_tree / "tools/consistency_rules.yml").write_text("retro_protocol: pre-F1\n")
    assert "Métricas retrospectivas provisionales" in card.render("2026-09-test")


@pytest.mark.parametrize(
    "change",
    [
        {"schema": True},
        {"status": "degraded"},
        {"expected_month": "2026-13"},
        {"status_since": "2026-02-30"},
        {"last_attempt": "2026-10-20"},
        {"n_index_links": True},
        {"failed_links": [3]},
    ],
)
def test_release_checks_reject_a_present_invalid_ingestion_record(tmp_path, change):
    state = json.loads((ROOT / "reports/governance/ingestion_state.json").read_text())
    state.update(change)
    feed = tmp_path / "reports/governance/ingestion_state.json"
    feed.parent.mkdir(parents=True)
    feed.write_text(json.dumps(state))
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    # Exercise the existing checker, rather than failing because a new file/API is absent.
    (contracts / "ingestion_state.json").write_text(
        json.dumps(
            {
                "contract_version": 1,
                "artifact": "reports/governance/ingestion_state.json",
                "kind": "json",
                "required_keys": {"schema": "int", "status": "str"},
            }
        )
    )
    assert check_contracts.check(tmp_path, contracts), "corrupt source state accepted as publishable"


def test_frozen_ingestion_record_is_valid():
    assert ingestion_state.read_state(ROOT / "reports/governance/ingestion_state.json") is not None


@pytest.fixture
def accreditation_boundary(card_tree, monkeypatch):
    """Unit doubles ONLY for the boundary after verification; the real reader is exercised separately."""
    from vp_model import reconciliation as rc
    from vp_model import vinculo_publicacion as vp

    (card_tree / "tools/consistency_rules.yml").write_text("retro_protocol: f2-causal\n")
    recon = card_tree / "recon"
    path = recon / rc.RUTA_REL
    path.parent.mkdir(parents=True)
    path.write_text("unit-boundary\n")
    inputs = {"governance/key_facts.json": b'{"n_obs": 2}'}
    panel = b"panel bytes\n"
    doc = {
        "status": "derived",
        "reconciliation_id": "recon-unit",
        "parent": {"campaign_id": "unit"},
        "inventory": {},
        "mother_layers": {},
        "sources": [],
        "outputs": {f"reports/{k}": hashlib.sha256(v).hexdigest() for k, v in inputs.items()},
        "panel_sha256": f"sha256:{hashlib.sha256(panel).hexdigest()}",
    }
    reads = []

    def read(root, *, sha256):
        assert root == recon
        assert sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
        reads.append(sha256)
        return doc

    monkeypatch.setattr(rc, "leer", read)
    monkeypatch.setattr(vp, "verificar_reconciliacion", lambda *a, **kw: (True, "unit verifier"))
    return {
        "root": card_tree,
        "recon": recon,
        "mother": card_tree / "mother",
        "inputs": inputs,
        "panel": panel,
        "doc": doc,
        "reads": reads,
        "vp": vp,
        "path": path,
    }


def _notice(m, **changes):
    from vp_model.retrospective_status import notice

    options = {"reconciliation_root": m["recon"], "parent_root": m["mother"]}
    options.update(changes)
    return notice(m["root"], m["inputs"], m["panel"], **options)


def test_verified_bytes_are_required_and_the_document_is_checked_twice(accreditation_boundary):
    m = accreditation_boundary
    assert "artefactos" in _notice(m) and "acreditaron" in _notice(m)
    assert len(m["reads"]) == 4  # two render-equivalent calls, each before/after verifier


def test_assembled_reconciliation_cannot_claim_accreditation(accreditation_boundary):
    m = accreditation_boundary
    m["doc"]["status"] = "assembled"
    with pytest.raises(ValueError, match="derived"):
        _notice(m)


def test_a_failing_real_verifier_is_not_treated_as_success(accreditation_boundary, monkeypatch):
    m = accreditation_boundary
    monkeypatch.setattr(m["vp"], "verificar_reconciliacion", lambda *a, **kw: (False, "corrupt source"))
    with pytest.raises(ValueError, match="no acreditada"):
        _notice(m)


@pytest.mark.parametrize("what", ["report", "missing", "panel"])
def test_different_rendered_bytes_are_refused(accreditation_boundary, what):
    m = accreditation_boundary
    if what == "report":
        m["inputs"]["governance/key_facts.json"] += b" "  # same parsed values, different bytes
    elif what == "missing":
        m["inputs"]["governance/key_facts.json"] = None
    else:
        m["panel"] += b" "
    with pytest.raises(ValueError, match="bytes renderizados"):
        _notice(m)


def test_a_rebound_reconciliation_document_is_refused(accreditation_boundary, monkeypatch):
    m = accreditation_boundary

    def verifier(*args, **kwargs):
        m["path"].write_text("changed during verification")
        return True, "unit verifier"

    monkeypatch.setattr(m["vp"], "verificar_reconciliacion", verifier)
    with pytest.raises(AssertionError):
        # The boundary reader double enforces the same hash; the real rc.leer does too.
        _notice(m)


def test_paired_roots_are_required(card_tree):
    from vp_model.retrospective_status import notice

    with pytest.raises(ValueError, match="juntos"):
        notice(card_tree, {}, None, reconciliation_root=card_tree)


def test_protocol_declaration_alone_cannot_accredit_a_supplied_reconciliation(card_tree):
    from vp_model.retrospective_status import notice

    with pytest.raises(ValueError, match="f2-causal"):
        notice(card_tree, {}, None, reconciliation_root=card_tree, parent_root=card_tree)


def test_render_uses_the_snapshot_it_validated(card_tree, monkeypatch):
    from vp_model import retrospective_status

    path = card_tree / "reports/governance/key_facts.json"
    path.write_text('{"n_obs": 1234567}')

    def inspect_snapshot(root, inputs, panel, **kw):
        assert inputs["governance/key_facts.json"] == b'{"n_obs": 1234567}'
        path.write_text('{"n_obs": 7654321}')
        return "> snapshot checked"

    monkeypatch.setattr(retrospective_status, "notice", inspect_snapshot)
    text = card.render("2026-09-test")
    assert "1,234,567" in text and "7,654,321" not in text


def test_render_refuses_a_link_instead_of_reading_another_tree(card_tree):
    path = card_tree / "reports/governance/key_facts.json"
    target = card_tree / "outside.json"
    target.write_text("{}")
    path.symlink_to(target)
    with pytest.raises(ValueError, match="enlace"):
        card.render("2026-09-test")


def test_feed_duplicate_keys_and_invalid_utf8_are_refused(tmp_path):
    with pytest.raises(ValueError, match="duplicada"):
        ingestion_state.parse_state('{"schema": 1, "schema": 2}')
    path = tmp_path / "feed.json"
    path.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="ilegible"):
        ingestion_state.read_state(path)
