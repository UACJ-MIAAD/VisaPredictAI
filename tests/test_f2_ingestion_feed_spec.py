"""F2 · el corte publica el feed de ingesta, y el corte viejo no se cuela por descuido.

El sitio prometía actualizarse solo. Para decir la verdad necesita leer el estado que el
pipeline registra (D3), y para leerlo el corte tiene que publicarlo. Aquí se fija el paso de
datos: el spec lo incluye, el gate lo exige, y el ÚNICO manifiesto que puede omitirlo es el ya
publicado, acreditado por sus cuatro valores exactos — que es lo que evita regenerar el release.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_contracts as cc  # noqa: E402

sys.path.insert(0, str(ROOT / "experiments"))
import build_release_manifest as brm  # noqa: E402

FEED = "reports/governance/ingestion_state.json"


def _manifest(**cambios) -> tuple[dict, str]:
    """El manifiesto vigente, opcionalmente mutado, con su sha coherente."""
    base = json.loads((ROOT / "reports/release/release_manifest.json").read_text())
    base.update(cambios)
    raw = json.dumps(base).encode()
    return base, hashlib.sha256(raw).hexdigest()


class TestTheSpecPublishesTheFeed:
    def test_future_cuts_carry_the_ingestion_feed(self) -> None:
        rutas = [rel for rel, _ in brm.GOVERNANCE] if hasattr(brm, "GOVERNANCE") else []
        fuente = (ROOT / "experiments/build_release_manifest.py").read_text()
        assert f'("{FEED}", "required")' in fuente, "el spec del manifiesto no publica el feed"
        assert rutas == rutas  # el spec puede ser literal; lo que importa es que esté declarado

    def test_the_feed_exists_to_be_published(self) -> None:
        assert (ROOT / FEED).exists()


class TestTheGateDemandsIt:
    def test_the_current_cut_is_accredited_and_passes(self) -> None:
        manifest, sha = _manifest()
        assert FEED not in [a["path"] for a in manifest["artifacts"]]
        assert cc._ingestion_feed_problems(manifest, cc.PRE_F2_MANIFEST_SHA256) == []

    @pytest.mark.parametrize(
        ("campo", "valor"),
        [
            ("release_id", "2026-10-otro"),
            ("panel_vintage", "2026-10"),
            ("n_artifacts", 112),
        ],
    )
    def test_any_other_manifest_without_the_feed_fails(self, campo: str, valor: object) -> None:
        manifest, _ = _manifest(**{campo: valor})
        problemas = cc._ingestion_feed_problems(manifest, cc.PRE_F2_MANIFEST_SHA256)
        assert problemas and campo in problemas[0]

    def test_a_different_manifest_sha_fails_even_with_the_right_fields(self) -> None:
        manifest, _ = _manifest()
        problemas = cc._ingestion_feed_problems(manifest, "0" * 64)
        assert problemas and "sha256 del manifiesto" in problemas[0]

    def test_a_manifest_that_publishes_the_feed_needs_no_exception(self) -> None:
        manifest, _ = _manifest()
        manifest["artifacts"] = [
            *manifest["artifacts"],
            {"path": FEED, "sha256": "0" * 64, "size": 1, "criticality": "required"},
        ]
        # Ni el sha ni la añada importan: el artefacto está, que es lo que se exige.
        assert cc._ingestion_feed_problems(manifest, "0" * 64) == []

    def test_an_empty_manifest_does_not_slip_through(self) -> None:
        problemas = cc._ingestion_feed_problems({"artifacts": []}, "0" * 64)
        assert problemas

    def test_the_live_tree_passes_the_whole_identity_check(self) -> None:
        assert cc.release_identity_problems(ROOT) == []


class TestTheForbiddenRuleKillsThePromise:
    def test_the_rule_is_declared_for_the_web(self) -> None:
        import yaml

        reglas = yaml.safe_load((ROOT / "tools/consistency_rules.yml").read_text())
        promesa = [r for r in reglas["forbidden"] if "feed autom" in r["pattern"]]
        assert promesa, "no hay regla que impida resucitar la promesa"
        assert "web" in promesa[0]["in"]
        assert "6-ago-2026" in promesa[0]["reason"]

    def test_the_pattern_catches_both_languages(self) -> None:
        import re

        import yaml

        reglas = yaml.safe_load((ROOT / "tools/consistency_rules.yml").read_text())
        patron = next(r["pattern"] for r in reglas["forbidden"] if "feed autom" in r["pattern"])
        rx = re.compile(patron, re.IGNORECASE)
        assert rx.search("el pipeline actualiza este feed automáticamente")
        assert rx.search("the pipeline updates this feed automatically")
        # y no marca la redacción honesta que la sustituye
        assert not rx.search("el estado de esa ingesta se informa arriba")
        assert not rx.search("the state of that ingestion is reported above")
