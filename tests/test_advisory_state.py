"""D2-B: estado visible de los advisories del cron (tools/advisory_state.py).

Cubre el contrato completo: marcadores estrictos, schema cerrado, consecutividad por
advisory, independencia entre ellos, estado corrupto preservado, escritura atómica,
derivación de N y de n_pairs_live, y las transiciones del issue único.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tools import advisory_state as adv

RUN = "run-1"
NOW = "2026-09-04T00:00:00+00:00"


def _state(champion: str, shadow: str, previous=None, run: str = RUN):
    return adv.next_state(previous, {"champion_challenger": champion, "shadow_freeze": shadow}, run, NOW)


# ---------------------------------------------------------------- marcadores estrictos
@pytest.mark.parametrize("raw", ["ok", "ok\n", " ok "])
def test_marker_ok_only_for_the_exact_value(raw: str) -> None:
    assert adv.marker_status(raw) == adv.OK


@pytest.mark.parametrize("raw", [None, "", "failed", "OK", "veredicto refrescado OK", "ok ok", "0", "true"])
def test_marker_anything_else_is_failed(raw: str | None) -> None:
    """N sale de los marcadores, nunca de prosa libre: solo el valor exacto cuenta."""
    assert adv.marker_status(raw) == adv.FAILED


def test_missing_marker_file_is_failed(tmp_path: Path) -> None:
    assert adv.read_marker(tmp_path / "no-existe.txt") == adv.FAILED


def test_failed_count_is_derived_and_bounded(tmp_path: Path) -> None:
    (tmp_path / "c").write_text("ok")
    (tmp_path / "s").write_text("failed")
    results = {"champion_challenger": adv.read_marker(tmp_path / "c"), "shadow_freeze": adv.read_marker(tmp_path / "s")}
    assert adv.failed_count(results) == 1
    assert adv.failed_count({"champion_challenger": "ok", "shadow_freeze": "ok"}) == 0
    assert adv.failed_count({"champion_challenger": "failed", "shadow_freeze": "failed"}) == 2
    assert 0 <= adv.failed_count(results) <= len(adv.ADVISORIES) == 2


# ------------------------------------------------------------------ ciclo del estado
def test_absent_state_initialises(tmp_path: Path) -> None:
    assert adv.read_state(tmp_path / "advisory_state.json") is None
    s = _state("failed", "ok", previous=None)
    assert s["advisories"]["champion_challenger"]["consecutive_failures"] == 1
    assert s["advisories"]["shadow_freeze"]["consecutive_failures"] == 0


def test_first_failure_does_not_open_an_issue() -> None:
    s1 = _state("failed", "ok")
    assert adv.issue_action(s1, None) == "none"


def test_second_consecutive_failure_opens_the_issue() -> None:
    s1 = _state("failed", "ok")
    s2 = _state("failed", "ok", previous=s1)
    assert s2["advisories"]["champion_challenger"]["consecutive_failures"] == 2
    assert adv.issue_action(s2, s1) == "open"


def test_further_failures_keep_asking_to_open_without_duplicating() -> None:
    s2 = _state("failed", "ok", previous=_state("failed", "ok"))
    s3 = _state("failed", "ok", previous=s2)
    assert s3["advisories"]["champion_challenger"]["consecutive_failures"] == 3
    assert adv.issue_action(s3, s2) == "open"  # el paso del workflow no crea si ya hay uno abierto


def test_recovery_resets_and_closes() -> None:
    s2 = _state("failed", "failed", previous=_state("failed", "failed"))
    s3 = _state("ok", "ok", previous=s2)
    assert all(e["consecutive_failures"] == 0 for e in s3["advisories"].values())
    assert adv.issue_action(s3, s2) == "close"


def test_recovery_without_previous_failures_does_nothing() -> None:
    s1 = _state("ok", "ok")
    s2 = _state("ok", "ok", previous=s1)
    assert adv.issue_action(s2, s1) == "none"


def test_advisories_are_independent() -> None:
    s1 = _state("failed", "ok")
    s2 = _state("ok", "failed", previous=s1)
    assert s2["advisories"]["champion_challenger"]["consecutive_failures"] == 0
    assert s2["advisories"]["shadow_freeze"]["consecutive_failures"] == 1
    assert adv.issue_action(s2, s1) == "none"


# ------------------------------------------------------------------- schema cerrado
def test_roundtrip_is_valid_and_closed(tmp_path: Path) -> None:
    p = tmp_path / "advisory_state.json"
    adv.write_state_atomic(p, _state("ok", "failed"))
    back = adv.read_state(p)
    assert back is not None
    assert set(back) == {"schema_version", "updated_at", "run_id", "advisories"}
    assert set(back["advisories"]) == set(adv.ADVISORIES)
    assert back["schema_version"] == adv.SCHEMA_VERSION


@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        "null",
        '"texto"',
        '{"schema_version": 1}',
        '{"schema_version": 2, "updated_at": "x", "run_id": "r", "advisories": {}}',
        '{"schema_version": true, "updated_at": "x", "run_id": "r", "advisories": {}}',
        '{"schema_version": 1, "updated_at": "x", "run_id": "r", "advisories": {"otro": {}}}',
        '{"schema_version": 1, "updated_at": "x", "run_id": "r", "advisories":'
        ' {"champion_challenger": {"status": "raro", "consecutive_failures": 0},'
        ' "shadow_freeze": {"status": "ok", "consecutive_failures": 0}}}',
        '{"schema_version": 1, "updated_at": "x", "run_id": "r", "advisories":'
        ' {"champion_challenger": {"status": "ok", "consecutive_failures": -1},'
        ' "shadow_freeze": {"status": "ok", "consecutive_failures": 0}}}',
        '{"schema_version": 1, "updated_at": "x", "run_id": "r", "advisories":'
        ' {"champion_challenger": {"status": "ok", "consecutive_failures": true},'
        ' "shadow_freeze": {"status": "ok", "consecutive_failures": 0}}}',
        '{"schema_version": 1, "updated_at": "", "run_id": "r", "advisories":'
        ' {"champion_challenger": {"status": "ok", "consecutive_failures": 0},'
        ' "shadow_freeze": {"status": "ok", "consecutive_failures": 0}}}',
        "no es json",
    ],
)
def test_corrupt_or_invalid_state_raises(tmp_path: Path, payload: str) -> None:
    p = tmp_path / "advisory_state.json"
    p.write_text(payload, encoding="utf-8")
    with pytest.raises(adv.CorruptStateError):
        adv.read_state(p)


def test_corrupt_state_is_preserved_by_the_cli(tmp_path: Path, capsys) -> None:
    """Un estado corrupto NO se sobrescribe en silencio: se conserva y se hace visible."""
    state = tmp_path / "advisory_state.json"
    original = '{"roto": true}'
    state.write_text(original, encoding="utf-8")
    (tmp_path / "c").write_text("ok")
    (tmp_path / "s").write_text("ok")
    rc = adv.main(
        [
            "update",
            "--state",
            str(state),
            "--marker",
            f"champion_challenger={tmp_path / 'c'}",
            "--marker",
            f"shadow_freeze={tmp_path / 's'}",
            "--promotion",
            str(tmp_path / "no-existe.json"),
            "--out",
            str(tmp_path / "summary.json"),
        ]
    )
    assert rc == 0
    assert state.read_text(encoding="utf-8") == original  # intacto
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["state_written"] is False and summary["corrupt"]
    assert summary["issue_action"] == "none"  # no bloquea ni dispara issues con estado dudoso
    assert "ESTADO CORRUPTO PRESERVADO" in summary["status_line"]


def test_write_is_atomic_and_leaves_no_temporary(tmp_path: Path) -> None:
    p = tmp_path / "advisory_state.json"
    adv.write_state_atomic(p, _state("ok", "ok"))
    adv.write_state_atomic(p, _state("failed", "ok"))
    assert [f.name for f in tmp_path.iterdir()] == ["advisory_state.json"]
    back = adv.read_state(p)
    assert back is not None and back["advisories"]["champion_challenger"]["status"] == "failed"


def test_write_rejects_an_invalid_state(tmp_path: Path) -> None:
    with pytest.raises(adv.CorruptStateError):
        adv.write_state_atomic(tmp_path / "s.json", {"schema_version": 1})


# ------------------------------------------------------------- gate prospectivo
def test_n_pairs_live_reads_the_real_decision() -> None:
    root = Path(__file__).resolve().parent.parent
    value = adv.n_pairs_live(root / "reports" / "governance" / "promotion_decision.json")
    assert isinstance(value, int) and value > 0


@pytest.mark.parametrize(
    "payload,expected",
    [
        ('{"n_pairs_live": 158}', 158),
        ('{"n_pairs_live": 0}', 0),
        ('{"n_pairs_live": "158"}', None),
        ('{"n_pairs_live": 12.5}', None),
        ('{"n_pairs_live": true}', None),
        ('{"n_pairs_live": -1}', None),
        ("{}", None),
        ("[]", None),
        ("no es json", None),
    ],
)
def test_n_pairs_live_is_honest_about_invalid_values(tmp_path: Path, payload: str, expected) -> None:
    p = tmp_path / "promotion_decision.json"
    p.write_text(payload, encoding="utf-8")
    assert adv.n_pairs_live(p) == expected


def test_n_pairs_live_absent_file_is_none(tmp_path: Path) -> None:
    assert adv.n_pairs_live(tmp_path / "no-existe.json") is None


# --------------------------------------------------------------------- CLI completa
def test_cli_derives_subject_suffix_and_lines(tmp_path: Path) -> None:
    state = tmp_path / "advisory_state.json"
    (tmp_path / "c").write_text("failed")
    (tmp_path / "s").write_text("ok")
    (tmp_path / "promotion_decision.json").write_text('{"n_pairs_live": 158}')
    args = [
        "update",
        "--state",
        str(state),
        "--marker",
        f"champion_challenger={tmp_path / 'c'}",
        "--marker",
        f"shadow_freeze={tmp_path / 's'}",
        "--promotion",
        str(tmp_path / "promotion_decision.json"),
        "--out",
        str(tmp_path / "summary.json"),
        "--run-id",
        "42",
    ]
    assert adv.main(args) == 0
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["failed_count"] == 1
    assert summary["subject_suffix"] == "[1 advisories fallidos]"
    assert summary["n_pairs_live"] == "158"
    assert "champion_challenger=failed" in summary["status_line"] and "shadow_freeze=ok" in summary["status_line"]
    assert summary["issue_action"] == "none" and summary["state_written"] is True
    written = adv.read_state(state)
    assert written is not None and written["run_id"] == "42"
    # segunda corrida con el mismo fallo: abre issue
    assert adv.main(args) == 0
    assert json.loads((tmp_path / "summary.json").read_text())["issue_action"] == "open"


def test_cli_rejects_unknown_or_missing_markers(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        adv.main(["update", "--state", str(tmp_path / "s.json"), "--marker", "otro=/tmp/x"])
    with pytest.raises(SystemExit):
        adv.main(["update", "--state", str(tmp_path / "s.json"), "--marker", "champion_challenger=/tmp/x"])


def test_state_file_is_published_by_the_model_stage() -> None:
    """El estado pertenece a la fase model y ya viaja en su allowlist vigente."""
    from tools import cron_publish

    assert any(
        "reports/governance/" == p or "reports/governance/advisory_state.json" == p
        for p in cron_publish.ALLOWLIST["model"]
    )
    assert not os.path.exists("reports/governance/advisory_state.json"), (
        "el estado se crea en una corrida real con rebuild; no debe fabricarse ni commitearse aquí"
    )
