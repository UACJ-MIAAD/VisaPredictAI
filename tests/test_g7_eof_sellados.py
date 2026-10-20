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


#: Réplica de `identify.file_is_text` (el filtro ``types: [text]`` del hook): texto = sin bytes de control binarios en
#: los primeros 1024. Contrastada con el hook real de pre-commit-hooks v5.0.0 (52 = 52 archivos, 16-oct).
_TEXTO = bytes(sorted({7, 8, 9, 10, 11, 12, 13, 27} | set(range(0x20, 0x100)) - {0x7F}))


def _es_texto(datos: bytes) -> bool:
    return not datos[:1024].translate(None, _TEXTO)


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
    return p + _sin_declarar(evidencia, versionados, leer)


def _sin_declarar(evidencia: dict, versionados: list[str], leer) -> list[str]:
    """Cada archivo versionado que el hook TOCARÍA está declarado: excluido por sellado, o preexistente fuera de G7
    con sus bytes. Una lista que sólo se verifica a sí misma no ve el sellado que falta (auditoría de B3-bis)."""
    p: list[str] = []
    tocaria = set()
    for f in versionados:
        datos = leer(f)
        if datos is not None and _es_texto(datos) and _motivo(datos):
            tocaria.add(f)
    previos = evidencia.get("preexisting", {})
    for f in sorted(tocaria - set(evidencia["files"]) - set(previos)):
        p.append(f"{f}: el hook lo reescribiría y no está declarado ni como sellado ni como preexistente")
    for f, meta in sorted(previos.items()):
        datos = leer(f)
        if datos is None or hashlib.sha256(datos).hexdigest() != meta["sha256"]:
            p.append(f"{f}: el preexistente cambió: si entra en el rango, decidir si se sella o se corrige")
        elif f not in tocaria:
            p.append(f"{f}: el hook ya no lo tocaría: sobra en preexistentes")
    for f in sorted(set(previos) & set(evidencia["files"])):
        p.append(f"{f}: no puede ser sellado y preexistente a la vez")
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
def test_un_sellado_omitido_de_la_lista_no_pasa(real: dict) -> None:
    """RED de la auditoría: quitar un sellado de la evidencia (y de la exclusión) tiene que verse."""
    evidencia = json.loads(EVIDENCIA.read_text(encoding="utf-8"))
    omitido = sorted(evidencia["files"])[0]
    evidencia["files"].pop(omitido)
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    hook = _hook(config, "end-of-file-fixer")
    hook["exclude"] = hook["exclude"].replace(re.escape(omitido) + "|", "", 1)
    p = problemas(config, evidencia, real["versionados"], _leer)
    assert any(omitido in x and "no está declarado" in x for x in p), p


def test_un_archivo_nuevo_que_el_hook_tocaria_no_pasa(real: dict) -> None:
    nuevo = "reports/campaign/nuevo_sellado_sin_salto.json"

    def leer(rel: str) -> bytes | None:
        return b'{"a": 1}' if rel == nuevo else _leer(rel)

    p = problemas(real["config"], real["evidencia"], [*real["versionados"], nuevo], leer)
    assert any(nuevo in x and "no está declarado" in x for x in p), p


def test_un_preexistente_que_cambia_no_pasa(real: dict) -> None:
    previo = sorted(real["evidencia"]["preexisting"])[0]

    def leer(rel: str) -> bytes | None:
        datos = _leer(rel)
        return datos + b"x" if rel == previo and datos is not None else datos

    p = problemas(real["config"], real["evidencia"], real["versionados"], leer)
    assert any(previo in x and "preexistente cambió" in x for x in p), p


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


def test_un_preexistente_que_el_hook_ya_no_tocaria_sobra(real: dict) -> None:
    evidencia = json.loads(EVIDENCIA.read_text(encoding="utf-8"))
    previo = sorted(evidencia["preexisting"])[0]
    arreglado = (_leer(previo) or b"") + b"\n"
    evidencia["preexisting"][previo]["sha256"] = hashlib.sha256(arreglado).hexdigest()

    def leer(rel: str) -> bytes | None:
        return arreglado if rel == previo else _leer(rel)

    p = problemas(real["config"], evidencia, real["versionados"], leer)
    assert any(previo in x and "sobra en preexistentes" in x for x in p), p


def test_un_archivo_no_puede_ser_sellado_y_preexistente(real: dict) -> None:
    evidencia = json.loads(EVIDENCIA.read_text(encoding="utf-8"))
    sellado = sorted(evidencia["files"])[0]
    evidencia["preexisting"][sellado] = dict(evidencia["files"][sellado])
    p = problemas(real["config"], evidencia, real["versionados"], _leer)
    assert any(sellado in x and "a la vez" in x for x in p), p
