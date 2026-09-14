#!/usr/bin/env python
"""Contrato de PUBLICABILIDAD del manifiesto de campana (fuente unica, fail-closed).

``experiments/sync_all.sh`` y sus tests comparten ESTA logica en vez de un ``grep``.
Publicar exige un manifiesto de campana que EXISTA, sea JSON valido y selle ``dirty``
como ``false`` BOOLEANO explicito. Fail-closed ante:

  * manifiesto ausente (actualmente lo esta y esta gitignored);
  * ilegible / vacio / malformado;
  * sin la clave ``dirty``;
  * ``dirty`` != ``false`` booleano ( ``true`` · ``"false"`` string · ``0`` · ``null`` ).

Motivo (auditoria 13-jul-2026 ronda 8): el ``grep '"dirty": *true'`` era FAIL-OPEN — no
cubria manifiesto ausente/vacio/malformado, ni ``{"dirty" : true}`` con espacios o saltos,
ni un cambio TOCTOU entre el chequeo y el ``dvc push``. Una campana CAMPAIGN_DIAGNOSTIC
(dirty=true) podia llegar a produccion. Este contrato lo cierra.

Uso:  python -m tools.campaign_manifest --assert-publishable reports/campaign/campaign_manifest.json
       (exit 0 = publicable · exit 7 = BLOQUEADO, con el motivo en stderr)
      python -m tools.campaign_manifest --assert-sealed <manifiesto>   (M74-E-R9: exit 1 si el sello no se acredita)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from vp_model.artifact_receipt import ReceiptError, loads_strict

#: ★ M74-E-R9 · esquemas CERRADOS del manifiesto de campaña y del sello de entradas (preflight).
MANIFEST_KEYS = frozenset({"campaign_id", "sha", "git_sha", "dirty", "started_at", "preflight", "preflight_sha256"})
SEAL_KEYS = frozenset(
    {"schema_version", "purpose", "git", "entrypoints", "inputs", "counts", "environment", "protocol"}
)


def _hex(valor: object, n: int) -> bool:
    return isinstance(valor, str) and len(valor) == n and all(c in "0123456789abcdef" for c in valor)


def seal_problems(path: str | Path) -> list[str]:
    """★ M74-E-R9 · única acreditación manifiesto ↔ sello: sha256 de sus bytes, identidad, esquemas; sin HEAD vivo."""
    p = Path(path)
    try:
        m = loads_strict(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, ReceiptError) as exc:
        return [f"manifiesto ilegible: {exc}"]
    if set(m) != MANIFEST_KEYS:
        return [f"manifiesto fuera del esquema cerrado {sorted(MANIFEST_KEYS)}"]
    cid, sha, sucio = m["campaign_id"], m["git_sha"], m["dirty"]
    if not (isinstance(cid, str) and cid and _hex(sha, 40) and m["sha"] == sha and isinstance(sucio, bool)):
        return ["manifiesto: campaign_id, sha/git_sha o dirty con tipo o valor inválido"]
    ruta = f"reports/logs/preflight_{cid}.json"
    if m["preflight"] != ruta or not _hex(m["preflight_sha256"], 64):
        return [f"manifiesto: el sello debe ser {ruta!r} con un sha256 hexadecimal"]
    sello = p.resolve().parents[2] / ruta
    if sello.is_symlink() or not sello.is_file():
        return [f"sello ausente o no regular: {ruta}"]
    crudo = sello.read_bytes()
    if hashlib.sha256(crudo).hexdigest() != m["preflight_sha256"]:
        return [f"el sello {ruta} no es el que registró el manifiesto (sha256 distinto)"]
    try:
        s = loads_strict(crudo.decode("utf-8"))
    except (ValueError, ReceiptError) as exc:
        return [f"sello ilegible: {exc}"]
    if set(s) != SEAL_KEYS or s["schema_version"] != 1 or s["schema_version"] is True:
        return ["sello fuera del esquema cerrado (claves o schema_version)"]
    probs: list[str] = []
    git, cuentas, entradas, entorno = s["git"], s["counts"], s["inputs"], s["environment"]
    if not isinstance(git, dict) or set(git) != {"head", "dirty"} or git["head"] != sha or git["dirty"] is not sucio:
        probs.append(f"sello de otra identidad: git={git!r} frente a git_sha {sha} y dirty {sucio}")
    for k in ("code", "data", "governance"):
        n = len(entradas[k]) if isinstance(entradas, dict) and isinstance(entradas.get(k), dict) else 0
        if not n or not isinstance(cuentas, dict) or cuentas.get(k) != n:
            probs.append(f"sello: inputs.{k} vacío o distinto de counts.{k}")
    if (
        not isinstance(entorno, dict)
        or not entorno
        or any(not isinstance(e, dict) or e.get("reproduces_lock") is not True for e in entorno.values())
    ):
        probs.append("sello: algún entorno no reproduce su lock")
    return probs


def publish_blocker(path: str | Path) -> str | None:
    """Motivo (str) por el que NO se puede publicar este manifiesto, o None si es publicable."""
    p = Path(path)
    if not p.exists():
        return f"falta el manifiesto de campana {p} (sin identidad sellada)"
    try:
        m = json.loads(p.read_text())
    except (json.JSONDecodeError, ValueError) as e:
        return f"manifiesto malformado ({type(e).__name__}: {e})"
    except OSError as e:
        return f"manifiesto ilegible ({type(e).__name__})"
    if not isinstance(m, dict):
        return "el manifiesto no es un objeto JSON"
    if "dirty" not in m:
        return "el manifiesto no sella la clave `dirty`"
    # `is not False` es DELIBERADO: rechaza true, "false" (string), 0 (== False pero no es
    # False), null. Solo el booleano JSON `false` autoriza publicar.
    if m["dirty"] is not False:
        return f"dirty={m['dirty']!r} (campana diagnostica) — re-lanza OFICIAL desde arbol limpio"
    problemas = seal_problems(p)
    if problemas:
        return "sello de entradas no acreditado: " + " · ".join(problemas)
    return None


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    modo = ap.add_mutually_exclusive_group(required=True)
    modo.add_argument("--assert-publishable", metavar="MANIFEST")
    modo.add_argument("--assert-sealed", metavar="MANIFEST")
    ns = ap.parse_args(argv[1:])
    if ns.assert_sealed:
        problemas = seal_problems(ns.assert_sealed)
        for x in problemas:
            print(f"SELLO NO ACREDITADO: {x}", file=sys.stderr)
        return 1 if problemas else 0
    blocker = publish_blocker(ns.assert_publishable)
    if blocker:
        print(f"PUBLISH BLOQUEADO: {blocker}", file=sys.stderr)
        return 7
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
