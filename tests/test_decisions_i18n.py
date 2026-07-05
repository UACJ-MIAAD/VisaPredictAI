"""Every master decision (cleaning + FE) must have an EN translation.

The web #fe section and the EN PDF read their English from
``vp_data.decisions_i18n.DECISIONS_EN`` (keyed by decision id). Spanish is
canonical; a decision without an EN entry ships es-only and silently falls
back — this test turns that silent hole into a failing gate, so adding a
decision without translating it can't slip through (AT5).

No heavy deps (no darts/torch) — runs in the plain-script cron gate too.
"""

import sys

from vp_data.cleaning import CLEANING_DECISIONS
from vp_data.decisions_i18n import DECISIONS_EN
from vp_model.feature_builder import FE_DECISIONS


def test_every_decision_has_en_translation():
    ids = [d["id"] for d in (*CLEANING_DECISIONS, *FE_DECISIONS)]
    assert len(ids) == len(set(ids)), "decision ids must be unique across both registries"
    missing = [i for i in ids if i not in DECISIONS_EN]
    assert not missing, f"decisiones sin traducción EN en decisions_i18n.py: {missing}"


def test_en_entries_are_complete():
    for did, tr in DECISIONS_EN.items():
        assert tr.get("title"), f"{did}: EN title vacío"
        assert tr.get("rationale"), f"{did}: EN rationale vacío"


if __name__ == "__main__":
    test_every_decision_has_en_translation()
    test_en_entries_are_complete()
    n = len({d["id"] for d in (*CLEANING_DECISIONS, *FE_DECISIONS)})
    print(f"OK — {n} decisiones, todas con traducción EN")
    sys.exit(0)
