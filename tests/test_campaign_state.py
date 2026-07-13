"""Maquina de estados endurecida: repros de los falsos verdes (auditoria ronda 10).

Cada repro demostraba un falso verde en la ronda 9; ahora TODOS deben fallar cerrado:
reset de campana terminal, carrera failed/computed, gates/reviewer/timestamps nulos, esquema
laxo (SHA no hex, campaign_id vacio, panel n/d, git_dirty string, revision, claves desconocidas).
"""

from __future__ import annotations

import pytest

from tools import campaign_state as cs

KW = dict(
    campaign_id="rederiv_deadbee_20260713",
    source_git_sha="a" * 40,
    git_dirty=False,
    panel_sha256="sha256:" + "b" * 64,
    started_at="2026-07-13T00:00:00+00:00",
)
TS = "2026-07-13T01:00:00+00:00"
RECEIPT = "c" * 64


def _seal(tmp_path):
    p = tmp_path / "campaign.json"
    cs.seal_running(p, **KW)
    return p


def _to_computed(p):
    return cs.mark_computed(p, completed_at=TS, input_gate="passed", output_gate="passed", consistency="passed")


# ── happy path completo ──
def test_full_lifecycle(tmp_path):
    p = _seal(tmp_path)
    assert cs.read(p)["status"] == "running" and cs.read(p)["revision"] == 0
    _to_computed(p)
    assert cs.read(p)["status"] == "computed" and cs.read(p)["revision"] == 1
    cs.mark_validated(p, validation_receipt_sha256=RECEIPT, reviewed_by="haowei", validated_at=TS, decision="approve")
    assert cs.read(p)["status"] == "validated"
    cs.mark_published(p, published_at=TS, release_sha="d" * 40)
    assert cs.read(p)["status"] == "published"
    assert not list(tmp_path.glob(".campaign.*.tmp"))


# ── P0: reset de campana existente/terminal ──
def test_seal_running_is_create_only(tmp_path):
    p = _seal(tmp_path)
    _to_computed(p)
    cs.mark_validated(p, validation_receipt_sha256=RECEIPT, reviewed_by="h", validated_at=TS, decision="ok")
    cs.mark_published(p, published_at=TS, release_sha="d" * 40)  # terminal published
    before = p.read_text()
    with pytest.raises(ValueError, match="ya existe"):
        cs.seal_running(p, **KW)  # reiniciar aborta
    assert p.read_text() == before  # ningun byte cambio


# ── P0: carrera failed/computed — el terminal no se pisa ──
def test_terminal_failed_not_overwritten_by_computed(tmp_path):
    p = _seal(tmp_path)
    cs.mark_failed(p, failed_stage="campaign", failed_at=TS, reason="kill")
    with pytest.raises(ValueError, match="no permitida"):
        _to_computed(p)  # computed no puede pisar failed
    assert cs.read(p)["status"] == "failed"


def test_published_is_terminal(tmp_path):
    p = _seal(tmp_path)
    _to_computed(p)
    cs.mark_validated(p, validation_receipt_sha256=RECEIPT, reviewed_by="h", validated_at=TS, decision="ok")
    cs.mark_published(p, published_at=TS, release_sha="d" * 40)
    with pytest.raises(ValueError, match="no permitida"):
        cs.mark_failed(p, failed_stage="x", failed_at=TS, reason="y")


# ── P0: no se llega a computed/validated con gates/reviewer/timestamps nulos ──
def test_computed_rejects_null_gates(tmp_path):
    p = _seal(tmp_path)
    with pytest.raises(ValueError, match="gates"):
        cs.mark_computed(p, completed_at=TS, input_gate="passed", output_gate=None, consistency="passed")


def test_computed_rejects_nonzero_exit(tmp_path):
    p = _seal(tmp_path)
    with pytest.raises(ValueError, match="exit_code"):
        cs.mark_computed(
            p, completed_at=TS, input_gate="passed", output_gate="passed", consistency="passed", exit_code=3
        )


def test_validated_rejects_empty_reviewer(tmp_path):
    p = _seal(tmp_path)
    _to_computed(p)
    with pytest.raises(ValueError, match="reviewed_by"):
        cs.mark_validated(p, validation_receipt_sha256=RECEIPT, reviewed_by="", validated_at=TS, decision="ok")


def test_validated_rejects_bad_receipt_hash(tmp_path):
    p = _seal(tmp_path)
    _to_computed(p)
    with pytest.raises(ValueError, match="receipt"):
        cs.mark_validated(p, validation_receipt_sha256="nothex", reviewed_by="h", validated_at=TS, decision="ok")


def test_cannot_skip_computed(tmp_path):
    p = _seal(tmp_path)
    with pytest.raises(ValueError, match="no permitida"):
        cs.mark_validated(p, validation_receipt_sha256=RECEIPT, reviewed_by="h", validated_at=TS, decision="ok")


# ── P1: esquema estricto ──
def test_seal_rejects_string_git_dirty(tmp_path):
    with pytest.raises(ValueError, match="git_dirty"):
        cs.seal_running(tmp_path / "c.json", **{**KW, "git_dirty": "false"})


def test_seal_rejects_non_hex_sha(tmp_path):
    with pytest.raises(ValueError):
        cs.seal_running(tmp_path / "c.json", **{**KW, "source_git_sha": "Z" * 40})


def test_seal_rejects_nd_panel(tmp_path):
    with pytest.raises(ValueError):
        cs.seal_running(tmp_path / "c.json", **{**KW, "panel_sha256": "n/d"})


def test_seal_rejects_empty_campaign_id(tmp_path):
    with pytest.raises(ValueError):
        cs.seal_running(tmp_path / "c.json", **{**KW, "campaign_id": "  "})


def test_seal_rejects_naive_timestamp(tmp_path):
    with pytest.raises(ValueError):
        cs.seal_running(tmp_path / "c.json", **{**KW, "started_at": "2026-07-13 00:00:00"})  # sin tz


def test_schema_rejects_unknown_key():
    base = {
        "schema_version": 2,
        "campaign_id": "c",
        "status": "running",
        "revision": 0,
        "source_git_sha": "a" * 40,
        "git_dirty": False,
        "panel_sha256": "sha256:" + "b" * 64,
        "started_at": TS,
    }
    assert cs.validate_schema(base) == []
    assert any("desconocidas" in x for x in cs.validate_schema({**base, "surprise": 1}))
    assert any("revision" in x for x in cs.validate_schema({**base, "revision": -1}))
    assert any("revision" in x for x in cs.validate_schema({k: v for k, v in base.items() if k != "revision"}))


def test_loads_rejects_duplicate_keys():
    with pytest.raises(ValueError, match="duplicada"):
        cs.loads('{"status": "running", "status": "validated"}')


# ── P1: CAS de revision + inmutables ──
def test_stale_revision_rejected(tmp_path):
    p = _seal(tmp_path)  # revision 0
    with pytest.raises(ValueError, match="revision stale"):
        cs.mark_computed(
            p, completed_at=TS, input_gate="passed", output_gate="passed", consistency="passed", expected_revision=5
        )


def test_transition_blocks_immutable_and_unknown(tmp_path):
    p = _seal(tmp_path)
    with pytest.raises(ValueError, match="inmutable"):
        cs._transition(p, "computed", {"source_git_sha": "e" * 40})
    with pytest.raises(ValueError, match="desconocida"):
        cs._transition(p, "computed", {"surprise": 1})
