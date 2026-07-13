"""Tests conductuales del agregador multi-semilla (auditoría 13-jul-2026 ronda 8).

EJECUTAN ``aggregate()`` sobre DataFrames sintéticos con las patologías que el auditor
reprodujo: semillas faltantes/sobrantes, NaN mezclado con finitos (que ``groupby.mean()``
omitía en silencio), y valores enormes que desbordan el promedio. Antes solo abortaba con
las 5 semillas Inf/NaN.
"""

from __future__ import annotations

import importlib.util
import math
import pathlib

import pandas as pd
import pytest

_P = pathlib.Path(__file__).resolve().parent.parent / "experiments" / "aggregate_seeds.py"
_spec = importlib.util.spec_from_file_location("aggregate_seeds_under_test", _P)
assert _spec and _spec.loader
agg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agg)


def _df(prefix, model, block, seed_to_vals):
    rows = []
    for seed, vals in seed_to_vals.items():
        for uid, v in enumerate(vals):
            rows.append(
                {"block": block, "model": model, "variant": f"{prefix}{seed}", "hold_mase": v, "unique_id": f"s{uid}"}
            )
    return pd.DataFrame(rows)


def _call(df):
    return agg.aggregate(df, prefix="auto_s", model="AutoBiTCN", block="family")


def test_five_finite_seeds_aggregate():
    df = _df("auto_s", "AutoBiTCN", "family", {i: [0.10 + 0.001 * i, 0.11, 0.12] for i in range(1, 6)})
    st = _call(df)
    assert st["n"] == 5
    assert 0.05 < st["mean"] < 0.30
    assert all(math.isfinite(st[k]) for k in ("mean", "sd", "se", "lo", "hi"))


def test_missing_seed_aborts():
    df = _df("auto_s", "AutoBiTCN", "family", {i: [0.1, 0.11] for i in range(1, 5)})  # solo 4
    with pytest.raises(SystemExit):
        _call(df)


def test_all_nan_seeds_abort():
    df = _df("auto_s", "AutoBiTCN", "family", {i: [float("nan")] * 3 for i in range(1, 6)})
    with pytest.raises(SystemExit):
        _call(df)


def test_mixed_finite_and_nan_aborts():
    # el bug original: groupby.mean() omitía el NaN de la semilla 3 y salía con un "número válido"
    seeds = {i: [0.1, 0.11, 0.12] for i in range(1, 6)}
    seeds[3] = [0.1, float("nan"), 0.12]
    with pytest.raises(SystemExit):
        _call(_df("auto_s", "AutoBiTCN", "family", seeds))


def test_huge_finite_values_overflow_abort():
    # cinco valores ~1e308 son finitos individualmente, pero su suma desborda el float64
    # (media = Inf) — el chequeo "después de agregar" lo caza.
    df = _df("auto_s", "AutoBiTCN", "family", {i: [1e308, 1e308, 1e308] for i in range(1, 6)})
    with pytest.raises(SystemExit):
        _call(df)
