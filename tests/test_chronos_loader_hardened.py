"""Carga HARDENED de Chronos en ambas rutas (P0R, ronda 10).

El smoke manual no sustituye a esto: interceptamos ``from_pretrained`` y verificamos que
la ruta principal (vp_model) pasa trust_remote_code=False + use_safetensors=True + revisión
inmutable; y cross-checkeamos que la ruta AWS (bundle standalone) usa los mismos 4 args y la
MISMA revisión literal (no puede importar vp_model, así que el test evita la deriva).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from vp_model.config import CHRONOS_MODEL, CHRONOS_REVISION

ROOT = Path(__file__).resolve().parent.parent
AWS = ROOT / "aws_gpu" / "chronos_lora.py"


def test_vp_loader_passes_hardened_kwargs(monkeypatch):
    chronos = pytest.importorskip("chronos")
    captured: dict = {}

    def fake_from_pretrained(model, **kw):
        captured["model"] = model
        captured.update(kw)
        return "PIPE"

    monkeypatch.setattr(chronos.BaseChronosPipeline, "from_pretrained", fake_from_pretrained)
    from vp_model.models import load_chronos_pipeline

    assert load_chronos_pipeline(CHRONOS_MODEL) == "PIPE"
    assert captured["trust_remote_code"] is False
    assert captured["use_safetensors"] is True
    assert captured["revision"] == CHRONOS_REVISION


def test_vp_loader_non_canonical_model_no_revision(monkeypatch):
    chronos = pytest.importorskip("chronos")
    captured: dict = {}
    monkeypatch.setattr(chronos.BaseChronosPipeline, "from_pretrained", lambda model, **kw: captured.update(kw) or "P")
    from vp_model.models import load_chronos_pipeline

    load_chronos_pipeline("otro/modelo")
    # trust_remote_code/safetensors SIEMPRE; revisión solo para el checkpoint canónico
    assert captured["trust_remote_code"] is False and captured["use_safetensors"] is True
    assert "revision" not in captured


def test_aws_route_is_hardened_and_revision_matches():
    src = AWS.read_text()
    assert "trust_remote_code=False" in src, "ruta AWS sin trust_remote_code=False"
    assert "use_safetensors=True" in src, "ruta AWS sin use_safetensors=True"
    assert 'kw["revision"] = CHRONOS_REVISION' in src or "revision" in src, "ruta AWS sin revisión"
    m = re.search(r'CHRONOS_REVISION\s*=\s*"([0-9a-f]{40})"', src)
    assert m, "aws_gpu/chronos_lora.py no declara CHRONOS_REVISION"
    assert m.group(1) == CHRONOS_REVISION, (
        f"revisión AWS {m.group(1)} != vp_model.config {CHRONOS_REVISION} (deriva de single-source)"
    )
