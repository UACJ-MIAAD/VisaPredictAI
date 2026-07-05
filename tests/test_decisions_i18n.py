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


def test_live_registries_are_fully_translated():
    """Cross-check against the LIVE registries, not the generated JSON. This
    catches "added a decision without translating it" IN THE PR that adds it,
    before fe_facts.json is even regenerated (blind audit r3: the fe_facts-only
    check was blind to additions). Imports darts via feature_builder, so it runs
    in the heavy model-tests job, not the light lint-and-test one — skipped when
    the modeling stack is absent."""
    import pytest

    pytest.importorskip("darts")
    from vp_data.cleaning import CLEANING_DECISIONS
    from vp_model.feature_builder import FE_DECISIONS

    ids = [d["id"] for d in (*CLEANING_DECISIONS, *FE_DECISIONS)]
    assert len(ids) == len(set(ids)), "decision ids must be unique across both registries"
    missing = [i for i in ids if i not in DECISIONS_EN]
    assert not missing, f"decisiones en los registros SIN traducción EN: {missing}"
    orphan = [k for k in DECISIONS_EN if k not in set(ids)]
    assert not orphan, f"entradas EN huérfanas (id ya no existe en los registros): {orphan}"


if __name__ == "__main__":
    test_shipped_facts_have_en_for_every_decision()
    test_registry_map_covers_every_shipped_id()
    test_en_entries_are_non_empty()
    n = len([d for key in ("cleaning_decisions", "fe_decisions") for d in _facts()[key]])
    print(f"OK — {n} decisiones en fe_facts.json, todas con traducción EN")
    sys.exit(0)
