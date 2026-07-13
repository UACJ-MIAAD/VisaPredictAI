"""Maquina de estados de campana-transaccion (auditoria 13-jul-2026 ronda 9)."""

from __future__ import annotations

import pytest

from tools import campaign_state as cs

KW = dict(
    campaign_id="rederiv_deadbee_20260713",
    source_git_sha="a" * 40,
    git_dirty=False,
    panel_sha256="sha256:" + "p" * 64,
    started_at="2026-07-13T00:00:00+00:00",
)


def _seal(tmp_path):
    p = tmp_path / "campaign.json"
    cs.seal_running(p, **KW)
    return p


def test_seal_running_is_valid(tmp_path):
    p = _seal(tmp_path)
    obj = cs.read(p)
    assert obj["status"] == "running"
    assert cs.validate_schema(obj) == []
    # no queda ningun temporal .campaign.*.tmp
    assert not list(tmp_path.glob(".campaign.*.tmp"))


def test_loads_rejects_duplicate_keys():
    with pytest.raises(ValueError, match="duplicada"):
        cs.loads('{"status": "running", "status": "validated"}')


def test_read_missing_or_malformed_is_none(tmp_path):
    assert cs.read(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert cs.read(bad) is None


def test_schema_rejects_bad_fields():
    assert any("schema_version" in x for x in cs.validate_schema({**_obj(), "schema_version": 1}))
    assert any("status" in x for x in cs.validate_schema({**_obj(), "status": "weird"}))
    assert any("git_dirty" in x for x in cs.validate_schema({**_obj(), "git_dirty": "false"}))
    assert any("40 caracteres" in x for x in cs.validate_schema({**_obj(), "source_git_sha": "abc"}))
    assert any("faltan campos" in x for x in cs.validate_schema({"schema_version": 2}))


def _obj():
    return {
        "schema_version": 2,
        "campaign_id": "c",
        "status": "running",
        "source_git_sha": "a" * 40,
        "git_dirty": False,
        "panel_sha256": "sha256:x",
        "started_at": "2026-07-13T00:00:00",
    }


# ── transiciones ──
def test_running_to_computed_ok(tmp_path):
    p = _seal(tmp_path)
    obj = cs.transition(p, "computed", input_gate="passed", output_gate="passed", consistency="passed")
    assert obj["status"] == "computed"
    assert cs.read(p)["input_gate"] == "passed"


def test_running_to_validated_blocked(tmp_path):
    p = _seal(tmp_path)
    with pytest.raises(ValueError, match="no permitida"):
        cs.transition(p, "validated")


def test_computed_to_validated_ok(tmp_path):
    p = _seal(tmp_path)
    cs.transition(p, "computed")
    obj = cs.transition(p, "validated", validated_at="2026-07-13T01:00:00", reviewed_by="haowei")
    assert obj["status"] == "validated"


def test_computed_to_published_blocked(tmp_path):
    p = _seal(tmp_path)
    cs.transition(p, "computed")
    with pytest.raises(ValueError, match="no permitida"):
        cs.transition(p, "published")


def test_failed_is_terminal(tmp_path):
    p = _seal(tmp_path)
    cs.transition(p, "failed", exit_code=3, failed_stage="campaign")
    with pytest.raises(ValueError, match="no permitida"):
        cs.transition(p, "computed")
    with pytest.raises(ValueError, match="no permitida"):
        cs.transition(p, "validated")


def test_cannot_mutate_immutable_field(tmp_path):
    p = _seal(tmp_path)
    with pytest.raises(ValueError, match="inmutable"):
        cs.transition(p, "computed", source_git_sha="b" * 40)
    with pytest.raises(ValueError, match="inmutable"):
        cs.transition(p, "computed", campaign_id="otra")


def test_validated_to_published_ok(tmp_path):
    p = _seal(tmp_path)
    cs.transition(p, "computed")
    cs.transition(p, "validated")
    obj = cs.transition(p, "published")
    assert obj["status"] == "published"
