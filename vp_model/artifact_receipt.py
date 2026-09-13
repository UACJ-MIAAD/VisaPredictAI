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
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


class ReceiptError(ValueError):
    """El artefacto no está acreditado, o lo está para otra corrida."""


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def panel_sha256_of(reports: Path) -> str:
    """El `panel_sha256` que la transacción selló. Fail-closed: sin campaña, no hay identidad.

    Recibe el directorio de **reports**, no la raíz: es el mismo mando que los consumidores usan
    para localizar el artefacto, y tener dos ganchos distintos es tener uno que se olvida.
    """
    txn = Path(reports) / "campaign" / "campaign.json"
    if not txn.is_file():
        raise ReceiptError(f"no hay transacción de campaña en {txn}: el artefacto no puede acreditarse")
    estado = json.loads(txn.read_text(encoding="utf-8"))
    valor = estado.get("panel_sha256")
    if not isinstance(valor, str) or not valor:
        raise ReceiptError("la transacción no sella un panel_sha256")
    return valor


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
        datos["expected_keys_sha256"] = hashlib.sha256(
            "\n".join("|".join(map(str, k)) for k in sorted(expected_keys)).encode()
        ).hexdigest()
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
    acta = json.loads(recibo.read_text(encoding="utf-8"))
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


def campaign_identity(reports: Path) -> tuple[str, str, str]:
    """`(campaign_id, code_sha, panel_sha256)` de la campaña ACTIVA. Fail-closed.

    Los consumidores corren DENTRO del runbook, que exporta `CAMPAIGN_ID`/`CAMPAIGN_SHA`. Fuera de
    una campaña no hay identidad que acreditar y leer el artefacto sería consumir una añada
    cualquiera: por eso esto levanta en vez de devolver valores por defecto.
    """
    cid = os.environ.get("CAMPAIGN_ID", "")
    sha = os.environ.get("CAMPAIGN_SHA", "")
    if not cid or not sha:
        raise ReceiptError(
            "sin CAMPAIGN_ID/CAMPAIGN_SHA en el entorno: los artefactos acreditados sólo se leen "
            "dentro de la campaña que los produjo. Corre esto desde el runbook, o expórtalos si "
            "estás re-ejecutando una etapa de una campaña concreta."
        )
    return cid, sha, panel_sha256_of(reports)
