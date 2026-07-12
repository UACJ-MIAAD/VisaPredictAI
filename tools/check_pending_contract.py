"""Contract check for the living pending list (Prompts/PENDIENTES.md).

Validates the cross-session contract of the single source of pendings:

  1. Every table row has a unique ID (numeric for open/closed-in-place rows,
     ``C<n>`` for the archived history rows).
  2. Every *vigente* row (state in {open, in-progress, paused}) carries all the
     required fields: estado, pendiente, quién, alta (ISO date), origen, and a
     notes cell that records ``Dep:`` and ``Next check:``.
  3. Row states belong to the allowed set {⬜, 🔄, ✅, ⏸}.
  4. The footer carries an ISO date: ``*Última actualización: YYYY-MM-DD``.

Exit code 0 when the contract holds, 1 otherwise (fail-closed: a malformed
table row is a violation, never a skip). Standalone stdlib script; not wired
to CI here (the orchestrator decides the wiring).

    ante/bin/python tools/check_pending_contract.py [path-to-PENDIENTES.md]
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent.parent / "Prompts" / "PENDIENTES.md"

ALLOWED_STATES = {"⬜", "🔄", "✅", "⏸"}
OPEN_STATES = {"⬜", "🔄", "⏸"}  # rows that must carry the full field contract
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FOOTER = re.compile(r"^\*Última actualización: (\d{4}-\d{2}-\d{2})")


def _cells(line: str) -> list[str]:
    """Split a markdown table row into stripped cells (drops edge empties)."""
    parts = [c.strip() for c in line.split("|")]
    if parts and parts[0] == "":
        parts = parts[1:]
    if parts and parts[-1] == "":
        parts = parts[:-1]
    return parts


def check(path: Path) -> list[str]:
    errors: list[str] = []
    if not path.exists():
        return [f"file not found: {path}"]
    lines = path.read_text(encoding="utf-8").splitlines()

    ids_open: dict[str, int] = {}
    ids_closed: dict[str, int] = {}
    footer_dates: list[str] = []

    for lineno, raw in enumerate(lines, start=1):
        line = raw.strip()
        m = FOOTER.match(line)
        if m:
            footer_dates.append(m.group(1))
            continue
        if not line.startswith("|"):
            continue
        cells = _cells(line)
        if not cells:
            continue
        head = cells[0]
        # header / separator rows
        if head in {"#", ""} or set(head) <= {"-", ":"}:
            continue

        if re.fullmatch(r"C\d+", head):  # archived history row: | C<n> | ... |
            if head in ids_closed:
                errors.append(f"L{lineno}: duplicated history ID {head} (first at L{ids_closed[head]})")
            else:
                ids_closed[head] = lineno
            continue

        if not re.fullmatch(r"\d+", head):
            continue  # not a pending row (e.g. legend tables), nothing to enforce

        if head in ids_open:
            errors.append(f"L{lineno}: duplicated pending ID #{head} (first at L{ids_open[head]})")
        else:
            ids_open[head] = lineno

        if len(cells) != 7:
            errors.append(
                f"L{lineno}: row #{head} has {len(cells)} cells, expected 7 (estado·pendiente·quién·alta·origen·notas)"
            )
            continue
        _, estado, pendiente, quien, alta, origen, notas = cells

        if estado not in ALLOWED_STATES:
            errors.append(f"L{lineno}: row #{head} state {estado!r} not in {sorted(ALLOWED_STATES)}")
            continue
        if estado not in OPEN_STATES:
            continue  # closed-in-place rows only need ID + valid state

        if not pendiente:
            errors.append(f"L{lineno}: row #{head} has an empty 'pendiente' cell")
        if not quien:
            errors.append(f"L{lineno}: row #{head} has an empty 'quién' (owner) cell")
        if not ISO_DATE.fullmatch(alta):
            errors.append(f"L{lineno}: row #{head} 'alta' is {alta!r}, expected ISO YYYY-MM-DD")
        else:
            y, mo, d = map(int, alta.split("-"))
            try:
                date(y, mo, d)
            except ValueError:
                errors.append(f"L{lineno}: row #{head} 'alta' {alta!r} is not a real calendar date")
        if not origen:
            errors.append(f"L{lineno}: row #{head} has an empty 'origen' cell")
        if "Dep:" not in notas:
            errors.append(f"L{lineno}: row #{head} notes lack a 'Dep:' (dependency) entry")
        if "Next check:" not in notas:
            errors.append(f"L{lineno}: row #{head} notes lack a 'Next check:' entry")

    if not footer_dates:
        errors.append("footer '*Última actualización: YYYY-MM-DD' with ISO date not found")
    else:
        for fd in footer_dates:
            y, mo, d = map(int, fd.split("-"))
            try:
                date(y, mo, d)
            except ValueError:
                errors.append(f"footer date {fd!r} is not a real calendar date")

    if not ids_open:
        errors.append("no pending rows found (the parser saw an empty list — contract cannot hold)")
    return errors


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else DEFAULT_PATH
    errors = check(path)
    if errors:
        print(f"PENDIENTES contract: {len(errors)} violation(s) in {path}")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"PENDIENTES contract OK ({path})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
