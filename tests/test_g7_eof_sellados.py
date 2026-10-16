"""M74-F · G7 · B3-bis — la excepción de `end-of-file-fixer` cubre EXACTAMENTE los JSON sellados que lo necesitan.

RED (auditoría de B3, 16-oct): el hook no declara etapas propias y hereda `pre-push`; sobre los 47 JSON de la madre
que no terminan en salto de línea, el push los reescribiría (o fallaría) y rompería su hash sellado. B3 lo evitó con
`SKIP=end-of-file-fixer` en el commit, que no cubre el push.

La exclusión vive en `.pre-commit-config.yaml` por nombre exacto y su lista es `docs/evidence/g7/eof_sellados.json`.
Estas pruebas funcionan en un clon limpio: exclusión = evidencia, ningún otro archivo versionado cae en ella, cada
archivo conserva el hash sellado y su motivo sigue siendo cierto. El contraste con el recibo de la reconciliación
que los selló es local.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
EVIDENCIA = ROOT / "docs" / "evidence" / "g7" / "eof_sellados.json"
CONFIG = ROOT / ".pre-commit-config.yaml"


def _hook(config: dict, hook_id: str) -> dict:
    hooks = [h for repo in config["repos"] for h in repo["hooks"] if h["id"] == hook_id]
    assert len(hooks) == 1, f"{hook_id}: {len(hooks)} declaraciones"
    return hooks[0]


def _motivo(datos: bytes) -> str | None:
    """Lo que `end-of-file-fixer` cambiaría en ``datos``; ``None`` si lo deja igual."""
    if not datos:
        return None
    if not datos.endswith(b"\n"):
        return "sin salto de línea final"
    if datos.endswith((b"\n\n", b"\r\n\r\n")):
        return "más de un salto de línea final"
    return None


def problemas(config: dict, evidencia: dict, versionados: list[str], leer) -> list[str]:
    """Vacío = la exclusión del hook es exactamente la lista sellada y cada archivo sigue siendo lo sellado."""
    p: list[str] = []
    patron = _hook(config, "end-of-file-fixer").get("exclude")
    if not patron:
        return ["end-of-file-fixer no declara la exclusión de los sellados"]
    rx = re.compile(patron)
    excluidos = {f for f in versionados if rx.search(f)}
    declarados = set(evidencia["files"])
    if excluidos != declarados:
        p.append(f"exclusión ≠ evidencia: de más {sorted(excluidos - declarados)[:3]}, "
                 f"de menos {sorted(declarados - excluidos)[:3]}")  # fmt: skip
    for f, meta in sorted(evidencia["files"].items()):
        datos = leer(f)
        if datos is None:
            p.append(f"{f}: no está en el árbol")
            continue
        if hashlib.sha256(datos).hexdigest() != meta["sha256"]:
            p.append(f"{f}: ya no son los bytes sellados")
        if _motivo(datos) != meta["reason"]:
            p.append(f"{f}: el hook ya no lo cambiaría por «{meta['reason']}»: la excepción sobra")
    for otro in ("trailing-whitespace", "check-yaml"):
        if _hook(config, otro).get("exclude"):
            p.append(f"{otro} declara exclusiones: la excepción es sólo para end-of-file-fixer")
    return p


def _versionados() -> list[str]:
    salida = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True).stdout
    return [f for f in salida.decode().split("\0") if f]


def _leer(rel: str) -> bytes | None:
    f = ROOT / rel
    return f.read_bytes() if f.is_file() else None


@pytest.fixture(scope="module")
def real() -> dict:
    return {
        "config": yaml.safe_load(CONFIG.read_text(encoding="utf-8")),
        "evidencia": json.loads(EVIDENCIA.read_text(encoding="utf-8")),
        "versionados": _versionados(),
    }


def test_la_exclusion_es_exactamente_la_lista_sellada(real: dict) -> None:
    assert problemas(real["config"], real["evidencia"], real["versionados"], _leer) == []


def test_la_lista_no_esta_vacia_y_cada_motivo_es_real(real: dict) -> None:
    assert len(real["evidencia"]["files"]) == 47
    assert {m["reason"] for m in real["evidencia"]["files"].values()} == {"sin salto de línea final"}


# ═══════════════════════════════ adversariales
def test_un_archivo_mas_en_la_exclusion_no_pasa(real: dict) -> None:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    hook = _hook(config, "end-of-file-fixer")
    hook["exclude"] = hook["exclude"].replace("^(", "^(\n  Makefile|", 1)
    assert any("de más ['Makefile']" in x for x in problemas(config, real["evidencia"], real["versionados"], _leer))


def test_un_patron_amplio_no_pasa(real: dict) -> None:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    _hook(config, "end-of-file-fixer")["exclude"] = r"^reports/.*\.json$"
    assert any("de más" in x for x in problemas(config, real["evidencia"], real["versionados"], _leer))


def test_un_sellado_que_cambia_o_ya_no_lo_necesita_no_pasa(real: dict) -> None:
    primero = next(iter(real["evidencia"]["files"]))

    def leer(rel: str) -> bytes | None:
        datos = _leer(rel)
        return datos + b"\n" if rel == primero and datos is not None else datos

    p = problemas(real["config"], real["evidencia"], real["versionados"], leer)
    assert any(primero in x and "bytes sellados" in x for x in p)
    assert any(primero in x and "sobra" in x for x in p)


def test_sin_exclusion_no_pasa(real: dict) -> None:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    _hook(config, "end-of-file-fixer").pop("exclude")
    assert problemas(config, real["evidencia"], real["versionados"], _leer)


# ═══════════════════════════════ contraste LOCAL con el recibo que los selló
def test_local_son_capas_selladas_de_la_reconciliacion(real: dict) -> None:
    recon = os.environ.get("VP_G7_RECONCILIACION")
    if not recon:
        pytest.skip("VP_G7_RECONCILIACION no apunta al reconciliation.json que los selló (contraste sólo local)")
    doc = json.loads(Path(recon).read_text(encoding="utf-8"))
    assert doc["reconciliation_id"] == real["evidencia"]["reconciliation_id"]
    for f, meta in real["evidencia"]["files"].items():
        assert doc["mother_layers"].get(f) == meta["sha256"], f
