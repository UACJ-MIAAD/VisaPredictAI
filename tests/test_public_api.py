"""Contrato de la API pública de vp_model (US D1 — código muerto, 2026-07-12).

Dos garantías:
(1) Anti-resurrección: las funciones borradas como código muerto (grep exhaustivo
    previo: cero consumidores en código, docs, .sh y workflows) NO deben reaparecer
    sin un consumidor real. Si alguna vuelve, este test obliga a documentar el
    consumidor y actualizar ``docs/dead_code_report.md``.
(2) ``eval_neuralforecast.global_summary`` se CONSERVA porque tiene consumidor
    documental (``aws_gpu/README.md`` y ``aws_gpu/GUIA_EC2.md`` la citan como snippet
    copy-paste): humo real sobre una fixture mínima + verificación de que el snippet
    de las guías sigue siendo compatible con las firmas actuales.

Se omite completo sin el extra ``model`` (mismo patrón que test_ensemble.py: el job
base de CI instala solo ``.[dev]``, sin darts/statsmodels).
"""

from __future__ import annotations

import importlib
import inspect
import re
from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("statsmodels")  # series_characterization/ensemble → extra `model`
pytest.importorskip("darts")  # report/eval_neuralforecast → vp_model.metrics → darts

# Con los centinelas presentes, un ImportError aquí es un BUG real (no skip).
ensemble = importlib.import_module("vp_model.ensemble")
eval_neuralforecast = importlib.import_module("vp_model.eval_neuralforecast")
palette = importlib.import_module("vp_model.palette")
report = importlib.import_module("vp_model.report")
series_characterization = importlib.import_module("vp_model.series_characterization")

REPO = Path(__file__).resolve().parent.parent
GUIDES = (REPO / "aws_gpu" / "README.md", REPO / "aws_gpu" / "GUIA_EC2.md")

# (módulo, símbolo borrado el 2026-07-12) — ver docs/dead_code_report.md.
DELETED = (
    (ensemble, "selection_table"),
    (palette, "country_color"),
    (report, "feature_tables_latex"),
    (report, "_CC"),  # dict auxiliar que solo consumía feature_tables_latex
    (series_characterization, "advanced_table"),
)


def test_modules_import_and_dead_functions_stay_dead() -> None:
    """Los 5 módulos importan y el código muerto borrado no resucita sin consumidor."""
    for mod, symbol in DELETED:
        assert not hasattr(mod, symbol), (
            f"{mod.__name__}.{symbol} fue borrada como código muerto (US D1, 2026-07-12; "
            "cero consumidores en código, docs y CI). Si la resucitas, añade el consumidor "
            "real y actualiza docs/dead_code_report.md."
        )


def _fixture_eval_df() -> pd.DataFrame:
    """Esquema mínimo que devuelve ``eval_global_deep`` (fila = variante×modelo×serie)."""
    rows = [
        {
            "variant": "diff",
            "model": model,
            "block": block,
            "country": country,
            "category": category,
            "hold_mase": mase,
            "hold_smape": 0.01,
            "hold_mae": 30.0,
        }
        for model, mase in (("PatchTST", 0.15), ("NHITS", 0.12))
        for country, category, block in (("mexico", "F1", "family"), ("india", "EB2", "employment"))
    ]
    return pd.DataFrame(rows)


def test_global_summary_smoke() -> None:
    """global_summary corre sobre una fixture mínima y rankea por MASE ascendente."""
    df = _fixture_eval_df()
    out = eval_neuralforecast.global_summary(df, block="family")
    assert not out.empty and len(out) == 2  # 1 variante × 2 modelos
    assert list(out.index.names) == ["variant", "model"]
    assert {"hold_mase", "hold_smape", "hold_mae"} <= set(out.columns)
    assert out["hold_mase"].is_monotonic_increasing  # ordenado: NHITS antes que PatchTST
    assert out.index.get_level_values("model")[0] == "NHITS"
    # El filtro por bloque separa familiar de empleo.
    assert len(eval_neuralforecast.global_summary(df, block="employment")) == 2


def test_guides_snippet_matches_current_api() -> None:
    """El snippet copy-paste de las guías GPU sigue siendo válido contra la API actual."""
    for guide in GUIDES:
        text = guide.read_text(encoding="utf-8")
        m = re.search(r"from vp_model\.eval_neuralforecast import ([\w, ]+)", text)
        assert m, f"{guide.name}: el snippet de import desapareció (¿se movió la guía?)"
        for name in (n.strip() for n in m.group(1).split(",")):
            assert hasattr(eval_neuralforecast, name), (
                f"{guide.name} cita eval_neuralforecast.{name}, que ya no existe: "
                "actualizar la guía o restaurar el símbolo."
            )
        call = re.search(r"global_summary\(eval_global_deep\((['\"])(\w+)\1\)\)", text)
        assert call, f"{guide.name}: la llamada del snippet cambió de forma; revisar la guía"
        # Las firmas actuales deben aceptar la llamada tal como la muestran las guías.
        inspect.signature(eval_neuralforecast.eval_global_deep).bind(call.group(2))
        inspect.signature(eval_neuralforecast.global_summary).bind(pd.DataFrame())
