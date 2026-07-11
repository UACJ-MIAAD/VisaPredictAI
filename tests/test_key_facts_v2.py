"""H2: la vista v2 de key_facts — aditiva, anotada y en equivalencia total con v1.

Contract tests de la aceptación: v1 permanece (dual-read; rollback = borrar "v2"),
cada hecho plano vive en EXACTAMENTE un namespace con valor idéntico, toda entrada v2
lleva unidad/población/fuente/añada, y la vista anidada jamás emite macros LaTeX.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KF = json.loads((ROOT / "reports" / "governance" / "key_facts.json").read_text())
TEX = (ROOT / "reports" / "latex" / "key_facts.tex").read_text()

NAMESPACES = {"data", "eda", "model", "backfill", "prospective", "governance"}


def _flat_scalars() -> dict:
    return {k: v for k, v in KF.items() if not k.startswith("_") and not isinstance(v, (list, dict))}


def test_v1_flat_interface_still_present() -> None:
    """Dual-read: los consumidores v1 (macros, guardián, model card) no pierden nada."""
    flat = _flat_scalars()
    for core in ("n_obs", "n_months", "prosp_mase", "prosp_n_scored", "n_models", "panel_vintage"):
        assert core in flat, f"clave v1 ausente: {core}"


def test_every_flat_fact_lives_in_exactly_one_namespace_with_equal_value() -> None:
    v2 = KF["v2"]
    assert set(v2) <= NAMESPACES
    homes: dict[str, list[str]] = {}
    for ns, entries in v2.items():
        for k, e in entries.items():
            homes.setdefault(k, []).append(ns)
            assert e["value"] == KF[k], f"v2.{ns}.{k} difiere de v1 ({e['value']!r} != {KF[k]!r})"
    flat = _flat_scalars()
    for k in flat:
        assert homes.get(k, []) and len(homes[k]) == 1, f"{k} debe vivir en EXACTAMENTE un namespace: {homes.get(k)}"
    assert set(homes) == set(flat)  # y nada fantasma en v2 que no exista en v1


def test_v2_entries_are_fully_annotated_and_single_vintage() -> None:
    vintage = KF["panel_vintage"]
    for ns, entries in KF["v2"].items():
        for k, e in entries.items():
            for field in ("unit", "population", "source", "vintage"):
                assert e.get(field), f"v2.{ns}.{k} sin '{field}'"
            assert e["vintage"] == vintage, f"v2.{ns}.{k} con añada mezclada"


def test_backfill_namespace_is_honestly_named_and_prospective_counts_live_zero() -> None:
    """A1/D1: lo puntuado hoy es backfill; el cero de pares live es dato de primera clase."""
    assert "prosp_mase" in KF["v2"]["backfill"]
    assert KF["v2"]["prospective"]["live_pairs_scored"]["value"] == KF["live_pairs_scored"]


def test_provenance_block_present() -> None:
    """H3: el corte que produjo las cifras es identificable máquina-a-máquina."""
    assert KF["_provenance"]["git_sha"] and KF["_provenance"]["pipeline_run_id"]
    assert KF["panel_hash_short"] and len(KF["panel_hash_short"]) == 12


def test_v2_emits_no_latex_macros() -> None:
    """La vista anidada no debe tocar el .tex (un dict emitía \\newcommand basura)."""
    assert "\\factV2" not in TEX
    assert TEX.count("\\newcommand") == sum(
        1 + (1 if isinstance(v, int) and v >= 1000 else 0) for v in _flat_scalars().values()
    )
