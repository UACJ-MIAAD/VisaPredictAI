"""G7 · B5b: el primer corte nuevo retira los tres puentes del corte legado ``2026-09-158ec972c234``.

Hasta el corte de la reconciliación, `check_contracts` aceptaba UN manifiesto sin bloque `identity` (PRE-D4), sin el feed
de ingesta (PRE-F2) y sin las cifras de cohortes (PRE-E5): el que el repositorio versionaba, acreditado por sus valores
exactos. En cuanto el repositorio emite un corte nuevo, la excepción ya no protege nada y sólo deja una puerta abierta a
esos bytes concretos. La fixture son esos bytes, tal como `4bed9c7` los versionaba: el control de que esta suite prueba
la puerta que importa, no un manifiesto inventado que ya fallaba antes.
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

FIXTURE = ROOT / "tests" / "fixtures" / "corte_legado_2026-09-158ec972c234"
RELEASE_LEGADO = "2026-09-158ec972c234"
SHA_MANIFIESTO_LEGADO = "8c362dbbacc245fdf4ba4b9835af096f81257ebc8f0a03c12a065484fbed3145"
SHA_TARJETA_LEGADA = "3cc175f4641547f7e19998495ed6d6422db53bb34bb8246e323a3be8ac1ee9fa"
FEED = "reports/governance/ingestion_state.json"
E5 = ("reports/governance/e5_facts.json", "reports/eval/series_cohorts.json")


def _arbol(raiz: Path, manifiesto: bytes, tarjeta: bytes) -> Path:
    for rel, datos in ((cc.MANIFEST_PATH, manifiesto), (cc.CARD_PATH, tarjeta)):
        (raiz / rel).parent.mkdir(parents=True, exist_ok=True)
        (raiz / rel).write_bytes(datos)
    return raiz


def _manifiesto_vivo() -> dict:
    return json.loads((ROOT / cc.MANIFEST_PATH).read_text())


def test_la_fixture_son_los_bytes_exactos_del_corte_legado() -> None:
    """Control: con estos bytes los tres puentes respondían `[]`; si cambian, la prueba de abajo no prueba nada."""
    manifiesto = (FIXTURE / "manifest.json").read_bytes()
    assert hashlib.sha256(manifiesto).hexdigest() == SHA_MANIFIESTO_LEGADO
    assert hashlib.sha256((FIXTURE / "card.md").read_bytes()).hexdigest() == SHA_TARJETA_LEGADA
    doc = json.loads(manifiesto)
    assert (doc["release_id"], doc["panel_vintage"], doc["n_artifacts"]) == (RELEASE_LEGADO, "2026-09", 111)
    rutas = {a["path"] for a in doc["artifacts"]}
    assert "identity" not in doc and FEED not in rutas and not rutas & set(E5)


def test_el_corte_legado_exacto_ya_no_se_acredita(tmp_path: Path) -> None:
    """Los tres puentes, cerrados: sus propios bytes exactos fallan por las tres razones."""
    raiz = _arbol(tmp_path, (FIXTURE / "manifest.json").read_bytes(), (FIXTURE / "card.md").read_bytes())
    problemas = cc.release_identity_problems(raiz)
    assert any("`identity`" in p for p in problemas), problemas
    assert any(FEED in p for p in problemas), problemas
    assert any(all(r in p for r in E5) for p in problemas), problemas


def test_ningun_valor_del_modulo_nombra_el_corte_legado() -> None:
    """Una excepción nominal se reconoce por su valor: ni el release_id ni los sha256 del corte legado siguen vivos."""
    valores = {v for v in vars(cc).values() if isinstance(v, str)}
    assert not valores & {RELEASE_LEGADO, SHA_MANIFIESTO_LEGADO, SHA_TARJETA_LEGADA}


def test_el_corte_del_repositorio_pasa_sin_puentes() -> None:
    assert cc.release_identity_problems(ROOT) == []
    manifiesto = _manifiesto_vivo()
    rutas = {a["path"] for a in manifiesto["artifacts"]}
    assert "identity" in manifiesto and manifiesto["release_id"] != RELEASE_LEGADO
    assert FEED in rutas and set(E5) <= rutas


@pytest.mark.parametrize("ruta", [FEED, *E5])
def test_un_corte_sin_un_artefacto_obligatorio_falla_sea_cual_sea_su_id(tmp_path: Path, ruta: str) -> None:
    """Sin excepción nominal, quitar el artefacto falla también con el release_id y la añada del corte legado."""
    for release_id in (RELEASE_LEGADO, "2026-10-otro"):
        manifiesto = _manifiesto_vivo()
        manifiesto["artifacts"] = [a for a in manifiesto["artifacts"] if a["path"] != ruta]
        manifiesto.update(release_id=release_id, panel_vintage="2026-09", n_artifacts=len(manifiesto["artifacts"]))
        raiz = _arbol(tmp_path / release_id, json.dumps(manifiesto).encode(), (ROOT / cc.CARD_PATH).read_bytes())
        problemas = cc.release_identity_problems(raiz)
        assert any(ruta in p for p in problemas), (release_id, problemas)
