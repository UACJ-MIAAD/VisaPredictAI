"""Maquinaria de acreditación de artefactos: escribir atómico, sellar, y verificar AL LEER.

`transported_forecasts.py` la estrenó para `ets`/`theta`. `persist_forecasts.py` la necesita para
`holdout_forecasts_{table}.csv`, que es el artefacto del que cuelgan **todos** los combinadores.
Copiarla habría sido la quinta lista de este repositorio con otro nombre, así que vive una vez.

**Qué acredita un recibo, y qué no.** Liga el artefacto a la corrida que lo produjo
(``campaign_id``, SHA del código, hash del panel), fija el **régimen** bajo el que se calculó y
guarda el sha256 del propio archivo. Al leer se re-verifica todo: un artefacto de otra añada, un
recibo de otra campaña o un CSV tocado después del sellado **no pasan**.

⚠️ Lo que un recibo NO puede decir es si los números son buenos. Dice de dónde vienen y que nadie
los tocó desde entonces. La cobertura —que estén TODOS los que deben estar— la comprueba cada
artefacto sobre su propio conjunto de claves esperado, porque sólo él sabe cuál es.

★ **M74-E-R2 · lo que R1 todavía se creía.** La auditoría reprodujo tres ataques contra el lector:

1. un CSV de **una sola fila** con un recibo que declaraba **5 400** fue aceptado — el lector
   verificaba identidad y hash, y luego **confiaba en la cobertura que declaraba el escritor**;
2. `campaign_identity` aceptó una transacción **`failed`** y un `CAMPAIGN_ID`/`CAMPAIGN_SHA` del
   entorno que **contradecían** `campaign.json`: sólo leía de ahí el `panel_sha256`;
3. `audit()` aceptó `actual=NaN` y `forecast=inf`.

Los tres tenían la misma forma: *acreditar la procedencia y dar por buena la sustancia*. Ahora la
identidad sale **exclusivamente** de una transacción `running` que debe coincidir con el entorno,
con el HEAD vivo y con el panel en disco; y el conjunto esperado se **recalcula al leer**.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any


class ReceiptError(ValueError):
    """El artefacto no está acreditado, o lo está para otra corrida."""


def _sin_duplicados(pares: list[tuple[str, Any]]) -> dict[str, Any]:
    vistas: set[str] = set()
    for k, _v in pares:
        if k in vistas:
            raise ReceiptError(f"clave duplicada en el JSON: {k!r}. Nadie esconde un segundo valor")
        vistas.add(k)
    return dict(pares)


def _no_finita(nombre: str) -> float:
    raise ReceiptError(f"constante JSON no finita: {nombre}")


def loads_strict(texto: str, *, finito: bool = False) -> dict[str, Any]:
    """``json.loads`` que RECHAZA claves duplicadas.

    ⚠️ `tools/campaign_state.py` tiene este mismo idioma de cinco líneas, y ADR-0001 prohíbe que
    `vp_model` importe `tools`. Se duplica el IDIOMA, no una autoridad: no hay aquí ninguna
    decisión que pueda divergir, sólo `object_pairs_hook`.
    """
    # ★ M74-E-R11 · con ``finito``, ``NaN``/``Infinity`` se rechazan al parsear, no después.
    obj = json.loads(texto, object_pairs_hook=_sin_duplicados, parse_constant=_no_finita if finito else None)
    if not isinstance(obj, dict):
        raise ReceiptError("el recibo no es un objeto JSON")
    return obj


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


#: Panel canónico: su hash tiene que coincidir con el que la transacción selló.
PANEL_REL = Path("data") / "processed" / "visa_panel_long.csv"


def read_running_transaction(reports: Path) -> dict[str, Any]:
    """La transacción de campaña, **y sólo si está `running`**.

    ★ R2: antes de aquí se leía `campaign.json` para sacarle el `panel_sha256` y nada más. Una
    campaña `failed` —como la de la escena preservada— servía igual de identidad. Una campaña que
    ya terminó, en cualquier desenlace, no está produciendo artefactos: leerlos bajo su nombre es
    exactamente la mezcla de añadas que este módulo existe para impedir.
    """
    txn = Path(reports) / "campaign" / "campaign.json"
    if not txn.is_file():
        raise ReceiptError(f"no hay transacción de campaña en {txn}: el artefacto no puede acreditarse")
    estado = loads_strict(txn.read_text(encoding="utf-8"))
    if estado.get("status") != "running":
        raise ReceiptError(
            f"la transacción está en {estado.get('status')!r} y no en 'running': una campaña que ya "
            "terminó no produce artefactos, así que tampoco presta su identidad para leerlos"
        )
    for clave in ("campaign_id", "source_git_sha", "panel_sha256"):
        valor = estado.get(clave)
        if not isinstance(valor, str) or not valor:
            raise ReceiptError(f"la transacción no sella un {clave} utilizable: {valor!r}")
    return estado


def panel_sha256_of(reports: Path) -> str:
    """El `panel_sha256` de la transacción `running`."""
    return str(read_running_transaction(reports)["panel_sha256"])


def expected_keys_sha256(claves: set[tuple]) -> str:
    """Huella del conjunto ESPERADO. Una sola implementación: la usa el sellado y la relectura, y
    dos versiones de esta función serían dos universos que parecen el mismo."""
    return hashlib.sha256("\n".join("|".join(map(str, k)) for k in sorted(claves)).encode()).hexdigest()


def receipt_path(artefacto: Path) -> Path:
    return artefacto.with_suffix(artefacto.suffix + ".receipt.json")


def replace_atomic(destino: Path, escribir) -> None:
    """Escribe por staging y promueve con `os.replace`.

    ⚠️ Nunca se escribe sobre el destino vivo. Un proceso que muera a mitad dejaría un artefacto
    truncado con aspecto de completo, que es exactamente la clase de daño que este módulo existe
    para evitar: `os.replace` es atómico dentro del mismo sistema de archivos.
    """
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_suffix(destino.suffix + ".tmp")
    try:
        escribir(tmp)
        os.replace(tmp, destino)
    finally:
        tmp.unlink(missing_ok=True)


def seal(
    destino: Path,
    *,
    schema: str,
    campaign_id: str,
    code_sha: str,
    panel_sha256: str,
    protocol: dict[str, Any],
    coverage: dict[str, Any],
    expected_keys: set[tuple] | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    """Sella el recibo DESPUÉS de que el artefacto esté en su sitio. Devuelve la ruta del recibo."""
    datos: dict[str, Any] = {
        "schema": schema,
        "campaign_id": campaign_id,
        "code_sha": code_sha,
        "panel_sha256": panel_sha256,
        "protocol": protocol,
        "coverage": coverage,
        "artifact_sha256": sha256_file(destino),
        **(extra or {}),
    }
    if expected_keys is not None:
        # El hash del conjunto ESPERADO, no del producido: así el recibo fija contra qué se midió
        # la cobertura y no puede reinterpretarse después con un universo más cómodo.
        datos["expected_keys_sha256"] = expected_keys_sha256(expected_keys)
        datos["n_expected_keys"] = len(expected_keys)
    recibo = receipt_path(destino)
    replace_atomic(recibo, lambda t: t.write_text(json.dumps(datos, indent=2, sort_keys=True) + "\n", encoding="utf-8"))
    return recibo


def verify(
    destino: Path,
    *,
    schema: str,
    campaign_id: str,
    code_sha: str,
    panel_sha256: str,
    protocol: dict[str, Any],
) -> dict[str, Any]:
    """Re-acredita AL LEER y devuelve el acta. Sin respaldo a un artefacto anterior.

    ★ La acreditación vive en la lectura. Un artefacto que sólo se verifica al escribirse queda
    a merced de cualquier cosa que pase después — y «después» incluye la corrida siguiente.
    """
    recibo = receipt_path(destino)
    if not destino.is_file() or not recibo.is_file():
        raise ReceiptError(
            f"falta {destino.name} o su recibo {recibo.name}. No hay respaldo a la añada anterior: "
            "un artefacto sin acreditar no se consume"
        )
    acta = loads_strict(recibo.read_text(encoding="utf-8"))
    if acta.get("schema") != schema:
        raise ReceiptError(f"recibo con esquema {acta.get('schema')!r}, se esperaba {schema!r}")
    for clave, esperado in (("campaign_id", campaign_id), ("code_sha", code_sha), ("panel_sha256", panel_sha256)):
        if acta.get(clave) != esperado:
            raise ReceiptError(
                f"{destino.name} dice {clave}={acta.get(clave)!r} y la campaña activa {esperado!r}: otra corrida"
            )
    real = sha256_file(destino)
    if acta.get("artifact_sha256") != real:
        raise ReceiptError(f"{destino.name} cambió desde el sellado (sha real {real[:12]}…)")
    if acta.get("protocol") != protocol:
        raise ReceiptError(f"el recibo declara otro régimen: {acta.get('protocol')} ≠ {protocol}")
    return acta


def _head_de(code_root: Path) -> str:
    fin = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=code_root, capture_output=True, text=True, check=False, timeout=60
    )
    if fin.returncode != 0:
        raise ReceiptError(f"no se pudo leer el HEAD vivo de {code_root}: {fin.stderr.strip()[:160]}")
    return fin.stdout.strip()


def campaign_identity(reports: Path, *, code_root: Path) -> tuple[str, str, str]:
    """`(campaign_id, code_sha, panel_sha256)` de la campaña ACTIVA, **derivados de la transacción**.

    ★ R2 · **cuatro cosas tienen que coincidir**, y antes no se comparaba ninguna:

    * la **transacción**, que es la autoridad y debe estar `running`;
    * el **entorno** (`CAMPAIGN_ID`/`CAMPAIGN_SHA`), que el runbook exporta — si contradice a la
      transacción, alguien está leyendo bajo el nombre de otra corrida;
    * el **HEAD vivo** del código que se está ejecutando: un commit a mitad de campaña deja los
      artefactos con SHAs mezclados, que es el bug de «identidades mezcladas» de julio;
    * el **panel en disco**, cuyo sha256 debe ser el que la transacción selló.

    El valor devuelto sale de la TRANSACCIÓN, nunca del entorno: el entorno se usa para detectar la
    contradicción, no como fuente.
    """
    estado = read_running_transaction(reports)
    cid, sha, panel = str(estado["campaign_id"]), str(estado["source_git_sha"]), str(estado["panel_sha256"])

    env_cid, env_sha = os.environ.get("CAMPAIGN_ID", ""), os.environ.get("CAMPAIGN_SHA", "")
    if not env_cid or not env_sha:
        raise ReceiptError(
            "sin CAMPAIGN_ID/CAMPAIGN_SHA en el entorno: los artefactos acreditados sólo se leen "
            "dentro de la campaña que los produjo. Corre esto desde el runbook."
        )
    if env_cid != cid or env_sha != sha:
        raise ReceiptError(
            f"el entorno dice campaign_id={env_cid!r}/sha={env_sha[:8]} y la transacción "
            f"{cid!r}/{sha[:8]}: se está leyendo bajo el nombre de otra corrida"
        )
    vivo = _head_de(code_root)
    if vivo != sha:
        raise ReceiptError(
            f"el HEAD vivo es {vivo[:8]} y la campaña selló {sha[:8]}: el código cambió a mitad de "
            "campaña y los artefactos quedarían con identidades mezcladas"
        )
    panel_real = Path(code_root) / PANEL_REL
    if not panel_real.is_file():
        raise ReceiptError(f"el panel sellado no existe en disco ({panel_real})")
    real = "sha256:" + sha256_file(panel_real)
    if real != panel:
        raise ReceiptError(
            f"el panel en disco es {real[:20]}… y la transacción selló {panel[:20]}…: "
            "los artefactos se calcularon sobre otros datos"
        )
    return cid, sha, panel
