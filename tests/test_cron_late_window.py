"""Execute the real Actions gate with an injected New York clock, without effects."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/freeze_and_rebuild.yml"


def _gate(tmp_path: Path, instant: str, event: str = "schedule", *, clock_fails: bool = False) -> tuple[bool, str]:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    script = workflow["jobs"]["gate"]["steps"][0]["run"]
    script = script.replace("${{ github.event_name }}", event)
    clock = datetime.fromisoformat(instant).astimezone(ZoneInfo("America/New_York"))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    date = bindir / "date"
    date.write_text(
        "#!/bin/bash\n"
        '[ "$TZ" = "America/New_York" ] || exit 23\n'
        '[ "$CLOCK_FAILS" != "true" ] || exit 25\n'
        'case "$1" in\n'
        '  +%H) printf "%s\\n" "$CLOCK_HOUR";;\n'
        '  +%u) printf "%s\\n" "$CLOCK_DAY";;\n'
        '  "+%u %H") printf "%s %s\\n" "$CLOCK_DAY" "$CLOCK_HOUR";;\n'
        "  *) exit 24;;\n"
        "esac\n"
    )
    date.chmod(0o755)
    output = tmp_path / "outputs"
    result = subprocess.run(
        ["/bin/bash", "-e", "-o", "pipefail", "-c", script],
        env={
            **os.environ,
            "PATH": str(bindir) + ":/usr/bin:/bin",
            "GITHUB_OUTPUT": str(output),
            "CLOCK_HOUR": clock.strftime("%H"),
            "CLOCK_DAY": clock.strftime("%u"),
            "CLOCK_FAILS": str(clock_fails).lower(),
        },
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    lines = output.read_text().splitlines()
    assert len(lines) == 1 and lines[0] in {"proceed=true", "proceed=false"}
    return lines[0] == "proceed=true", result.stdout


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        ("2026-10-21T16:00:00+00:00", True),  # EDT noon
        ("2026-10-21T17:00:00+00:00", True),
        ("2026-10-21T19:00:00+00:00", True),  # observed 15 h delay
        ("2026-10-21T20:00:00+00:00", True),  # observed 16 h delay
        ("2026-10-22T03:59:00+00:00", True),  # still Wednesday, 23:59 NY
        ("2026-10-22T04:00:00+00:00", False),  # new day: no catch-up at midnight
        ("2026-11-02T16:00:00+00:00", False),  # EST off-season trigger, 11 h
        ("2026-11-02T17:00:00+00:00", True),  # EST noon
        ("2026-11-02T22:00:00+00:00", True),
        ("2026-10-24T16:00:00+00:00", False),  # Saturday
        ("2026-10-25T20:00:00+00:00", False),  # Sunday
    ],
)
def test_scheduled_gate_accepts_late_weekdays_only(tmp_path: Path, instant: str, expected: bool) -> None:
    assert _gate(tmp_path, instant)[0] is expected


def test_manual_ingestion_keeps_its_weekend_midnight_entry(tmp_path: Path) -> None:
    assert _gate(tmp_path, "2026-10-25T04:00:00+00:00", "workflow_dispatch")[0]


def test_delayed_gate_reports_the_actual_time(tmp_path: Path) -> None:
    proceed, log = _gate(tmp_path, "2026-10-21T20:00:00+00:00")
    assert proceed and "16" in log and "Nueva York" in log


def test_two_delayed_triggers_are_serialized_without_cancellation() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    assert workflow["concurrency"] == {"group": "${{ github.workflow }}", "cancel-in-progress": False}
    assert workflow["jobs"]["update"]["if"] == "needs.gate.outputs.proceed == 'true'"


def test_clock_failure_does_not_become_a_successful_skip(tmp_path: Path) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        _gate(tmp_path, "2026-10-21T20:00:00+00:00", clock_fails=True)
    assert not (tmp_path / "outputs").exists()
