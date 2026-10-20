"""M74-F · G7 · B5a — la EVIDENCIA del cómputo de una campaña, separada de su estado de publicación.

`campaign.json` cuenta dos cosas: lo que se calculó (identidad, puertas, cierre) y en qué punto de la publicación
está (validada, publicada). Las complementarias, las reconciliaciones y la evidencia copiada fijan el sha256 del
`campaign.json` en `computed`; validar o publicar reescriben el archivo y ese hash dejaría de existir.

Por eso, al pasar a `validated`, la máquina de estados (`tools.campaign_state`) guarda una copia **create-only** y
**exacta** de esos bytes (`SNAPSHOT`) y registra su hash en `computed_sha256`. Este lector es el único que responde
«¿cuál es la evidencia del cómputo de esta madre?» y la ACREDITA al leer:

* en `computed`, el propio archivo (y, si una validación interrumpida dejó la copia, tiene que ser idéntica);
* en `validated` o `published`, la copia, sólo si su hash es `computed_sha256`, es un `computed` válido y cada clave
  suya —salvo las que la publicación hace avanzar— es IDÉNTICA a la del estado vivo, cuya revisión es posterior.

No hay comparación permisiva: el hash que se devuelve es el de unos bytes concretos, y el estado vivo tiene que
describir el mismo cómputo campo a campo.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from vp_model.campaign_schema import validate_schema

#: Junto a `campaign.json`.
SNAPSHOT = "campaign.computed.json"
#: Lo que la publicación hace avanzar sobre la evidencia del cómputo.
_AVANZAN = frozenset({"status", "revision", "consistency"})
#: Lo único que la publicación AÑADE al estado vivo.
_PUBLICACION = frozenset({"validated_at", "reviewed_by", "decision", "validation_receipt_sha256",
                          "validation_receipt_path", "computed_sha256", "published_at", "release_sha"})  # fmt: skip
#: Estados cuya evidencia del cómputo se puede leer.
ADMITIDOS = frozenset({"computed", "validated", "published"})


class EvidenciaError(ValueError):
    """La evidencia del cómputo no se acredita."""


def _objeto(datos: bytes, nombre: str) -> dict[str, Any]:
    def sin_duplicadas(pares: list[tuple[str, Any]]) -> dict[str, Any]:
        claves = [k for k, _ in pares]
        if len(claves) != len(set(claves)):
            raise EvidenciaError(f"{nombre}: claves duplicadas")
        return dict(pares)

    try:
        obj = json.loads(datos, object_pairs_hook=sin_duplicadas)
    except (ValueError, UnicodeDecodeError) as exc:
        raise EvidenciaError(f"{nombre} ilegible: {exc}") from exc
    if not isinstance(obj, dict):
        raise EvidenciaError(f"{nombre} no es un objeto")
    return obj


def evidencia_computada(ruta: Path) -> tuple[str, dict[str, Any]]:
    """``(sha256 de la evidencia del cómputo, estado vivo)`` de la transacción en ``ruta``. Fail-closed."""
    ruta = Path(ruta)
    datos = ruta.read_bytes()
    vivo = _objeto(datos, ruta.name)
    estado = vivo.get("status")
    copia = ruta.parent / SNAPSHOT
    if estado == "computed":
        if copia.exists() and copia.read_bytes() != datos:
            raise EvidenciaError(f"{copia.name} existe y no es este cómputo: alguien la escribió fuera de la máquina")
        return hashlib.sha256(datos).hexdigest(), vivo
    if estado not in ADMITIDOS:
        raise EvidenciaError(f"una transacción en {estado!r} no tiene evidencia de cómputo admitida")
    if not copia.is_file():
        raise EvidenciaError(f"una transacción en {estado!r} exige {copia.name} y no está")
    bytes_copia = copia.read_bytes()
    sha = hashlib.sha256(bytes_copia).hexdigest()
    if vivo.get("computed_sha256") != sha:
        raise EvidenciaError(f"{copia.name} no es la evidencia que el estado sella (computed_sha256)")
    computado = _objeto(bytes_copia, copia.name)
    problemas = validate_schema(computado)
    if problemas or computado.get("status") != "computed":
        raise EvidenciaError(f"{copia.name} no es un estado `computed` válido: {problemas or computado.get('status')}")
    # La máquina siembra en `null` las claves de publicación desde `running`: en el cómputo tienen que seguir vacías,
    # y sólo ellas pueden llenarse después. Todo lo demás, idéntico.
    precocinadas = sorted(k for k in _PUBLICACION if computado.get(k) is not None)
    distintas = sorted(
        k for k in computado if k not in _AVANZAN and k not in _PUBLICACION and computado[k] != vivo.get(k)
    )
    nuevas = sorted(k for k in set(vivo) - set(computado) if k not in _PUBLICACION)
    if precocinadas or distintas or nuevas:
        raise EvidenciaError(
            f"el estado vivo no describe el mismo cómputo: distintas {distintas}, nuevas {nuevas}, "
            f"de publicación ya llenas en el cómputo {precocinadas}"
        )
    rev_vivo, rev_copia = vivo.get("revision"), computado.get("revision")
    if not (isinstance(rev_vivo, int) and isinstance(rev_copia, int) and rev_vivo > rev_copia):
        raise EvidenciaError(f"la revisión viva ({rev_vivo!r}) no es posterior a la del cómputo ({rev_copia!r})")
    return sha, vivo
