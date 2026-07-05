"""Every master decision (cleaning + FE) must carry an EN translation.

The web #fe section and the EN PDF read their English from
``vp_data.decisions_i18n.DECISIONS_EN`` (keyed by decision id). Spanish is
canonical; a decision without an EN entry ships es-only and silently falls
back — this test turns that silent hole into a failing gate (AT5).

Sources the decision ids from the committed ``reports/fe/fe_facts.json`` (the
exact artifact the web consumes) so it needs neither darts nor torch — it runs
in the light ``lint-and-test`` CI job and the plain-script cron gate.
"""

import json
import sys
from pathlib import Path

from vp_data.decisions_i18n import DECISIONS_EN

FACTS = Path(__file__).resolve().parents[1] / "reports" / "fe" / "fe_facts.json"


def _facts() -> dict:
    return json.loads(FACTS.read_text())


def test_shipped_facts_have_en_for_every_decision():
    facts = _facts()
    holes = [
        d["id"]
        for key in ("cleaning_decisions", "fe_decisions")
        for d in facts[key]
        if not d.get("title_en") or not d.get("rationale_en")
    ]
    assert not holes, f"decisiones en fe_facts.json sin title_en/rationale_en: {holes}"


def test_registry_map_covers_every_shipped_id():
    facts = _facts()
    ids = [d["id"] for key in ("cleaning_decisions", "fe_decisions") for d in facts[key]]
    assert len(ids) == len(set(ids)), "decision ids must be unique across both registries"
    missing = [i for i in ids if i not in DECISIONS_EN]
    assert not missing, f"ids sin entrada en decisions_i18n.DECISIONS_EN: {missing}"


def test_en_entries_are_non_empty():
    for did, tr in DECISIONS_EN.items():
        assert tr.get("title"), f"{did}: EN title vacío"
        assert tr.get("rationale"), f"{did}: EN rationale vacío"


if __name__ == "__main__":
    test_shipped_facts_have_en_for_every_decision()
    test_registry_map_covers_every_shipped_id()
    test_en_entries_are_non_empty()
    n = len([d for key in ("cleaning_decisions", "fe_decisions") for d in _facts()[key]])
    print(f"OK — {n} decisiones en fe_facts.json, todas con traducción EN")
    sys.exit(0)
