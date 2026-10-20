"""M74-E-R10 · el sello se acredita por su CONTENIDO, no sólo por sus bytes y su identidad.

Auditoría del 14-sep (sin informe escrito): R9 cerraba el esquema SUPERIOR y nada más. Un sello con
`purpose`, `entrypoints` y `protocol` nulos, hashes nulos, un entorno inventado y `started_at=17` se
acreditaba (`seal_problems == []`, `publish_blocker is None`), y la propia fixture lo llamaba «mínimo
válido». Además `publish_blocker` leía el manifiesto dos veces. Ahora el esquema cerrado y recursivo vive
en `tools/campaign_seal_schema.json` y el sello de referencia de las pruebas es uno REAL.
"""

from __future__ import annotations

import ast
import json
import shutil
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import tools.campaign_manifest as cm  # noqa: E402
from tools import campaign_preflight as pf  # noqa: E402


def test_control_el_sello_real_se_acredita(tmp_path: Path, campana_sellada) -> None:
    assert cm.seal_problems(campana_sellada(tmp_path)) == []
    assert cm.publish_blocker(campana_sellada(tmp_path / "publicable")) is None


def test_RED_la_falsificacion_exacta_de_la_auditoria(tmp_path: Path, campana_sellada) -> None:
    """★ purpose/entrypoints/protocol nulos, hashes nulos y un entorno inventado: R9 los acreditaba."""
    sello = campana_sellada.sello()
    sello |= {
        "purpose": None,
        "entrypoints": None,
        "protocol": None,
        "environment": {"inventado": {"reproduces_lock": True}},
    }
    for k in ("code", "data", "governance"):
        sello["inputs"][k] = dict.fromkeys(sello["inputs"][k])
    m = campana_sellada(tmp_path, sello=sello)
    problemas = cm.seal_problems(m)
    for campo in ("purpose", "entrypoints", "protocol", "inputs.code", "environment"):
        assert any(f"sello.{campo}" in x for x in problemas), campo
    assert cm.publish_blocker(m)


_SELLO = {
    "purpose_otro": lambda s: s.update(purpose="cualquier cosa"),
    "protocol_clave_extra": lambda s: s["protocol"].update(inventada=1),
    "protocol_sin_clave": lambda s: s["protocol"].pop("horizons"),
    "protocol_tipo": lambda s: s["protocol"]["run_metadata"].update(seed="42"),
    "entrypoints_ruta_rara": lambda s: s["entrypoints"]["scripts"].append("../../etc/passwd"),
    "hash_no_hex": lambda s: s["inputs"]["code"].update({next(iter(s["inputs"]["code"])): "z" * 64}),
    "arbol_mal_formado": lambda s: s["inputs"]["data"].update({"data/raw": "tree:abc"}),
    "anchors_vacios": lambda s: s["inputs"].update(governance_anchors={}),
    "counts_como_texto": lambda s: s["counts"].update(code=str(s["counts"]["code"])),
    "counts_descuadrados": lambda s: s["counts"].update(code=s["counts"]["code"] + 1),
    "entorno_de_mas": lambda s: s["environment"].update(otro=s["environment"]["ante"]),
    "entorno_de_menos": lambda s: s["environment"].pop("ante_nf"),
    "entorno_sin_clave": lambda s: s["environment"]["ante"].pop("freeze_sha256"),
    "entorno_venv_ajeno": lambda s: s["environment"]["ante"].update(venv="ante_nf"),
    "entorno_lock_ajeno": lambda s: s["environment"]["ante_nf"].update(lock="model-cpu.txt"),
    "entorno_no_reproduce": lambda s: s["environment"]["ante_nf"].update(reproduces_lock=False),
    "entorno_no_declarado": lambda s: s["environment"]["ante"].update(undeclared=["torch"]),
    "schema_version_bool": lambda s: s.update(schema_version=True),
}


@pytest.mark.parametrize("perturbacion", sorted(_SELLO))
def test_RED_cada_desvio_del_contenido_se_rechaza(tmp_path: Path, campana_sellada, perturbacion: str) -> None:
    sello = campana_sellada.sello()
    _SELLO[perturbacion](sello)
    m = campana_sellada(tmp_path, sello=sello)
    assert cm.seal_problems(m) and cm.publish_blocker(m)


_MANIFIESTO = {
    "started_at_numero": ("started_at", 17),
    "started_at_imposible": ("started_at", "2026-13-40T99:99:99Z"),
    "campaign_id_con_barras": ("campaign_id", "rederiv_aaaaaaa_20260914T000000/../x"),
    "campaign_id_de_otro_sha": ("campaign_id", "rederiv_bbbbbbb_20260914T000000"),
    "ruta_con_traversal": ("preflight", "reports/logs/../../preflight_rederiv_aaaaaaa_20260914T000000.json"),
    "sha_en_mayusculas": ("git_sha", "A" * 40),
    "dirty_como_texto": ("dirty", "false"),
}


@pytest.mark.parametrize("perturbacion", sorted(_MANIFIESTO))
def test_RED_el_manifiesto_tiene_gramatica_y_tipos_exactos(tmp_path: Path, campana_sellada, perturbacion: str) -> None:
    m = campana_sellada(tmp_path)
    datos = json.loads(m.read_text(encoding="utf-8"))
    campo, valor = _MANIFIESTO[perturbacion]
    datos[campo] = valor
    m.write_text(json.dumps(datos), encoding="utf-8")
    assert cm.seal_problems(m)


def test_RED_publicar_lee_el_manifiesto_una_sola_vez(tmp_path: Path, monkeypatch, campana_sellada) -> None:
    """★ Tras la primera lectura (limpio) se promueve atómicamente una campaña sucia coherente consigo misma.
    Con dos lecturas, `dirty` se comprobaba sobre la limpia y el sello sobre la sucia: publicable."""
    limpio = campana_sellada(tmp_path / "limpio")
    campana_sellada(tmp_path / "sucio", dirty=True)
    leer, lecturas = Path.read_text, []

    def lectura(self, *a, **k):
        texto = leer(self, *a, **k)
        if self == limpio:
            lecturas.append(1)
            if len(lecturas) == 1:
                shutil.copytree(tmp_path / "sucio" / "reports", tmp_path / "limpio" / "reports", dirs_exist_ok=True)
        return texto

    monkeypatch.setattr(Path, "read_text", lectura)
    assert cm.publish_blocker(limpio) is not None
    assert len(lecturas) == 1


def test_el_esquema_describe_lo_que_el_preflight_emite_y_queda_sellado() -> None:
    arbol = ast.parse((RAIZ / "tools" / "campaign_preflight.py").read_text(encoding="utf-8"))
    seal = next(n for n in arbol.body if isinstance(n, ast.FunctionDef) and n.name == "seal")
    ret = next(n.value for n in ast.walk(seal) if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict))
    claves = [k.value for k in ret.keys if isinstance(k, ast.Constant)]
    assert set(claves) == set(cm.ESQUEMA["seal"])
    proposito = next(v for k, v in zip(claves, ret.values, strict=True) if k == "purpose")
    assert isinstance(proposito, ast.Constant) and proposito.value == cm.ESQUEMA["seal"]["purpose"]["="]
    assert "tools/campaign_seal_schema.json" in pf.GOVERNANCE_PLAIN
