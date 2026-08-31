"""Source-ingestion state feed (D3, schema v1) — the ONE record of how the
bulletin source responded, shared by the cron (SES line, single blocked-issue),
the watchdog and, later, the website (B4).

Written by ``pipeline.freeze_snapshots`` only when the SEMANTIC content
changes (an identical payload is a no-op, and there is deliberately no
``last_attempt`` or per-run timestamp, so a stable situation produces zero
daily churn — a status flip, a month becoming overdue or the index growing DO
rewrite). The file is versioned through the cron's data-publish allowlist —
a change is committed even when there is no rebuild, through the shared data
push site — and stays OUT of the release manifest (it must never move
``release_id``).

Schema v1 is CLOSED (exactly ``KEYS``; ``validate`` fails on anything extra)
and both ends are fail-closed: an invalid state never reaches disk, and a file
that EXISTS but does not parse or validate raises on read (bytes preserved for
forensics) — only an ABSENT file means "nothing recorded" and lets consumers
keep their previous behavior. Semantics: ``status_since`` = day the current
status streak was first derived (faithful once the state persists in a
commit); ``last_success_date`` = start of the latest recorded ok streak.
``expected_month`` follows an explicit cutoff policy: the bulletin for month
M+1 publishes around mid-M, so from day 15 the NEXT month's bulletin is
already expected. Stdlib-pure on purpose (runs in the cron's slim data phase).
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path

SCHEMA_VERSION = 1
STATUSES = ("ok", "blocked", "partial", "offline")
KEYS = (
    "schema",
    "status",
    "panel_vintage",
    "expected_month",
    "missing_months",
    "status_since",
    "last_success_date",
    "n_index_links",
    "failed_links",
    "reason",
)
_HUMAN_ACTION = "accion humana: make ingest-manual FILE=<html> y despues workflow_dispatch con skip_freeze"


def read_state(path: Path) -> dict | None:
    """ABSENT file -> None (nothing recorded). A file that EXISTS but does not
    parse or does not pass the closed-schema validation RAISES: corruption is a
    real failure, never silently equated to "no state" (which would let the
    writer pave over the forensic evidence)."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"feed de ingesta ilegible ({path}): {exc}") from exc
    if not isinstance(state, dict):
        raise ValueError(f"feed de ingesta ilegible ({path}): no es un objeto JSON")
    problems = validate(state)
    if problems:
        raise ValueError(f"feed de ingesta fuera de contrato ({path}): {problems}")
    return state


def panel_vintage(panel_path: Path) -> str | None:
    """``max(bulletin_date)[:7]`` scanning the CSV by hand (no pandas)."""
    try:
        fh = Path(panel_path).open(encoding="utf-8")
    except OSError:
        return None
    with fh:
        header = fh.readline().strip().split(",")
        if "bulletin_date" not in header:
            return None
        idx = header.index("bulletin_date")
        best = ""
        for line in fh:
            cols = line.rstrip("\n").split(",")
            if len(cols) > idx and cols[idx] > best:
                best = cols[idx]
    return best[:7] or None


def _months_between(after: str, upto: str) -> list[str]:
    """Months strictly after ``after`` up to ``upto`` inclusive (YYYY-MM)."""
    ya, ma = int(after[:4]), int(after[5:7])
    yb, mb = int(upto[:4]), int(upto[5:7])
    months = []
    while (ya, ma) < (yb, mb):
        ma += 1
        if ma == 13:
            ya, ma = ya + 1, 1
        months.append(f"{ya:04d}-{ma:02d}")
    return months


EXPECTED_CUTOFF_DAY = 15  # el boletín del mes M+1 se publica a mediados de M


def _expected_month(today: date) -> str:
    """Explicit cutoff policy: before day 15 the current month's bulletin is
    the expectation; from day 15 the NEXT month's is already due (an August 31
    with a July panel is missing August AND September)."""
    if today.day < EXPECTED_CUTOFF_DAY:
        return f"{today:%Y-%m}"
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return f"{year:04d}-{month:02d}"


def derive_state(
    *,
    status: str,
    reason: str,
    n_index_links: int | None,
    failed_links: list[str],
    panel_vintage: str | None,
    today: date,
    previous: dict | None,
) -> dict:
    """Pure derivation of the schema-v1 payload (clock injected, no I/O)."""
    expected = _expected_month(today)
    missing = _months_between(panel_vintage, expected) if panel_vintage else []
    prev_status = previous.get("status") if previous else None
    since = previous["status_since"] if previous and prev_status == status else f"{today:%Y-%m-%d}"
    if status == "ok":
        last_success = since
    else:
        last_success = previous.get("last_success_date") if previous else None
    return {
        "schema": SCHEMA_VERSION,
        "status": status,
        "panel_vintage": panel_vintage,
        "expected_month": expected,
        "missing_months": missing,
        "status_since": since,
        "last_success_date": last_success,
        "n_index_links": n_index_links,
        "failed_links": list(failed_links),
        "reason": reason,
    }


def _is_int(v: object) -> bool:
    # bool es subclase de int: True se colaría como 1 en un isinstance ingenuo
    return isinstance(v, int) and not isinstance(v, bool)


def _is_month(s: object) -> bool:
    return isinstance(s, str) and len(s) == 7 and _parses(s, "%Y-%m")


def _is_day(s: object) -> bool:
    return isinstance(s, str) and len(s) == 10 and _parses(s, "%Y-%m-%d")


def _parses(s: str, fmt: str) -> bool:
    try:
        datetime.strptime(s, fmt)
    except ValueError:
        return False
    return True


def validate(state: dict) -> list[str]:
    """Closed-schema check ([] = valid): exact key set, enums, formats, types."""
    problems = []
    extra, absent = set(state) - set(KEYS), set(KEYS) - set(state)
    if extra:
        problems.append(f"llaves fuera del schema v1: {sorted(extra)}")
    if absent:
        problems.append(f"llaves requeridas ausentes: {sorted(absent)}")
        return problems
    if not _is_int(state["schema"]) or state["schema"] != SCHEMA_VERSION:
        problems.append(f"schema {state['schema']!r} != {SCHEMA_VERSION}")
    if state["status"] not in STATUSES:
        problems.append(f"status {state['status']!r} fuera de {STATUSES}")
    if state["panel_vintage"] is not None and not _is_month(state["panel_vintage"]):
        problems.append(f"panel_vintage inválido: {state['panel_vintage']!r}")
    if not _is_month(state["expected_month"]):
        problems.append(f"expected_month inválido: {state['expected_month']!r}")
    if not _is_day(state["status_since"]):
        problems.append(f"status_since inválido: {state['status_since']!r}")
    if state["last_success_date"] is not None and not _is_day(state["last_success_date"]):
        problems.append(f"last_success_date inválido: {state['last_success_date']!r}")
    if not isinstance(state["missing_months"], list) or not all(_is_month(m) for m in state["missing_months"]):
        problems.append("missing_months debe ser lista de YYYY-MM")
    if state["n_index_links"] is not None and not _is_int(state["n_index_links"]):
        problems.append("n_index_links debe ser int o null")
    if not isinstance(state["failed_links"], list) or not all(isinstance(x, str) for x in state["failed_links"]):
        problems.append("failed_links debe ser lista de strings")
    if not isinstance(state["reason"], str):
        problems.append("reason debe ser str")
    return problems


def write_if_transition(state: dict, path: Path) -> bool:
    """Persist the state ONLY when the semantic payload differs from what the
    file records (identical = no-op, so a stable situation never churns
    commits). Doubly fail-closed: an invalid NEW state raises before touching
    disk, and an EXISTING corrupt/invalid file raises via ``read_state`` with
    its bytes preserved. The write is atomic (tmp + ``os.replace``)."""
    problems = validate(state)
    if problems:
        raise ValueError(f"estado de ingesta inválido: {problems}")
    path = Path(path)
    if read_state(path) == state:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)
    return True


def summary_line(state: dict | None) -> str:
    """One honest line for the SES heartbeat (and any human-facing surface)."""
    if state is None:
        return "n/d (sin estado de ingesta registrado)"
    pv = state["panel_vintage"] or "n/d"
    missing = ", ".join(state["missing_months"]) or "ninguno"
    if state["status"] == "blocked":
        return (
            f"BLOQUEADA ({state['reason']}) desde {state['status_since']} — panel en {pv}, "
            f"falta(n): {missing}; {_HUMAN_ACTION}"
        )
    if state["status"] == "offline":
        return f"SIN RED ({state['reason']}) — panel en {pv}"
    if state["status"] == "partial":
        return f"PARCIAL: {len(state['failed_links'])} link(s) fallidos (ver logs) — panel en {pv}"
    if state["missing_months"]:
        return f"OK pero falta(n) {missing} (panel en {pv}; ¿boletín aún no publicado?)"
    return f"OK (panel al día en {pv})"
