"""Propiedades de los hashes estables de contenido (auditoria 13-jul-2026 ronda 9)."""

from __future__ import annotations

import math

from tools import campaign_hashing as ch


def test_grid_hash_is_order_independent():
    a = [("mx_F1", "2026-01-01"), ("in_F4", "2026-02-01")]
    assert ch.grid_sha256(a) == ch.grid_sha256(list(reversed(a)))


def test_truth_hash_differs_on_y_same_grid():
    grid = [("mx_F1", "2026-01-01"), ("in_F4", "2026-02-01")]
    a = [(u, d, 1.0) for u, d in grid]
    b = [(u, d, 2.0) for u, d in grid]
    # misma grilla, distinta y: el hash de grilla coincide pero el de verdad NO
    assert ch.grid_sha256(grid) == ch.grid_sha256(grid)
    assert ch.truth_sha256(a) != ch.truth_sha256(b)


def test_finite_mask_hash_differs_on_mask():
    rows_a = [("mx_F1", "2026-01-01", True), ("in_F4", "2026-02-01", True)]
    rows_b = [("mx_F1", "2026-01-01", True), ("in_F4", "2026-02-01", False)]
    assert ch.finite_mask_sha256(rows_a) != ch.finite_mask_sha256(rows_b)


def test_hexfloat_handles_nan_inf():
    assert ch.hexfloat(float("nan")) == "nan"
    assert ch.hexfloat(float("inf")) == "+inf"
    assert ch.hexfloat(float("-inf")) == "-inf"
    # round-trip exacto para finitos y para nan
    assert float.fromhex(ch.hexfloat(0.1234567890123)) == 0.1234567890123
    assert math.isnan(float.fromhex(ch.hexfloat(float("nan"))))


def test_artifact_file_hash_matches_recompute(tmp_path):
    f = tmp_path / "m.pkl"
    f.write_bytes(b"weights")
    assert ch.artifact_tree_sha256(f) == ch.artifact_tree_sha256(f)
    assert ch.artifact_tree_sha256(f).startswith("sha256:")


def test_artifact_dir_hash_stable_and_sensitive(tmp_path):
    d = tmp_path / "model_dir"
    (d / "sub").mkdir(parents=True)
    (d / "a.bin").write_bytes(b"AAA")
    (d / "sub" / "b.bin").write_bytes(b"BBB")
    h1 = ch.artifact_tree_sha256(d)
    # recomputar da lo mismo (independiente del orden del walk)
    assert ch.artifact_tree_sha256(d) == h1
    # cambiar un archivo cambia el hash del arbol
    (d / "sub" / "b.bin").write_bytes(b"CCC")
    assert ch.artifact_tree_sha256(d) != h1


def test_panel_sha256_present_and_missing(tmp_path):
    p = tmp_path / "panel.csv"
    p.write_text("uid,ds,y\n")
    assert ch.panel_sha256(p).startswith("sha256:")
    assert ch.panel_sha256(tmp_path / "nope.csv") == "n/d"
