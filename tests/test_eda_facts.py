"""Contrato del censo EDA (``reports/eda/eda_facts.json``).

Valida el ARTEFACTO versionado (no lo recomputa: el censo tarda ~2 min por las 74
pruebas de estacionariedad). Dos garantías: (1) regla #0 — los conteos compartidos
con ``key_facts.json`` son IDÉNTICOS (mismo insumo, mismo número); (2) estructura —
las claves que consumen la galería, el PDF y la web existen y son coherentes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EDA = ROOT / "reports" / "eda" / "eda_facts.json"
KEY = ROOT / "reports" / "governance" / "key_facts.json"

SHARED_KEYS = (
    "n_obs",
    "n_series_structural",
    "n_series_with_F",
    "n_series_evaluable",
    "n_obs_F",
    "pct_trainable_F",
    "date_first",
    "date_last",
)


def test_eda_facts_matches_key_facts():
    eda, key = json.loads(EDA.read_text()), json.loads(KEY.read_text())
    for k in SHARED_KEYS:
        assert eda["panel"][k] == key[k], f"regla #0 violada: {k} eda={eda['panel'][k]} key={key[k]}"
    # claves del censo que el guardián vigila en prosa (audit 3-jul H3): misma derivación
    assert eda["panel"]["n_months"] == key["n_months"]
    assert len(eda["retro_events"]) == key["n_retro_events"]
    assert eda["dv"]["n_rows"] == key["dv_n_rows"]
    assert round(eda["panel"]["pct_frozen"]) == key["pct_frozen"]
    assert round(eda["panel"]["pct_retro"], 1) == key["pct_retro"]


def test_eda_facts_structure_is_consumable():
    eda = json.loads(EDA.read_text())
    p = eda["panel"]
    # vintage = último boletín del panel (el PDF y la web lo muestran)
    assert eda["vintage"] == p["date_last"]
    # censo completo: una fila por serie estructural, con perfil mínimo
    assert len(eda["series"]) == p["n_series_structural"]
    required = {"country", "block", "category", "table", "n_F", "n_retro", "evaluable"}
    assert required.issubset(eda["series"][0].keys())
    # las pruebas formales cubren EXACTAMENTE las evaluables (verdict no-nulo)
    with_verdict = [s for s in eda["series"] if s.get("verdict")]
    assert len(with_verdict) == p["n_series_evaluable"]
    # el régimen particiona el panel completo
    assert sum(eda["regime"].values()) == p["n_obs"]
    # insumos de la galería presentes y no vacíos
    for k in ("retro_events", "fad_dff_gap", "backlog_today"):
        assert eda[k], f"{k} vacío"
    assert len(eda["monthly_advance_median"]) == 12
    # DV: hecho descriptivo presente (1,647 filas canónicas al 3-jul-2026, crece con el tiempo)
    assert eda["dv"]["n_rows"] >= 1600


if __name__ == "__main__":
    test_eda_facts_matches_key_facts()
    test_eda_facts_structure_is_consumable()
    print("OK — eda_facts.json consistente con key_facts.json y estructuralmente consumible")
    sys.exit(0)
