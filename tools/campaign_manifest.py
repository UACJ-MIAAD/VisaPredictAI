#!/usr/bin/env python
"""Contrato de PUBLICABILIDAD del manifiesto de campaña y de su sello de entradas (fuente única, fail-closed).

``experiments/sync_all.sh``, el runbook y el gate de completitud comparten ESTA lógica. Publicar exige un
manifiesto que exista, sea JSON sin claves duplicadas y selle ``dirty`` como ``false`` booleano (ronda 8: el
``grep`` era fail-open), y cuyo sello de entradas se acredite por bytes, identidad y CONTENIDO bajo el esquema
cerrado de ``campaign_seal_schema.json`` (M74-E-R9 y R10).

Uso:  python -m tools.campaign_manifest --assert-publishable <manifiesto>   (exit 0 publicable · 7 bloqueado)
      python -m tools.campaign_manifest --assert-sealed <manifiesto>        (exit 1 si el sello no se acredita)
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path

from vp_model.artifact_receipt import ReceiptError, loads_strict

#: ★ M74-E-R10 · esquema cerrado y recursivo del manifiesto y del sello, como DATOS junto a su lector.
ESQUEMA = json.loads(Path(__file__).with_name("campaign_seal_schema.json").read_text(encoding="utf-8"))
_TIPOS = {"int": int, "float": float, "bool": bool, "str": str}


def shape_problems(v: object, f: object, ruta: str) -> list[str]:
    """Contrasta ``v`` con la forma ``f`` del esquema; la gramática está documentada en el propio JSON."""
    if isinstance(f, dict) and set(f) == {"="}:
        return [] if type(v) is type(f["="]) and v == f["="] else [f"{ruta}: {v!r} en vez de {f['=']!r}"]
    if isinstance(f, dict):
        mala = "*clave" in f and isinstance(v, dict) and any(not re.fullmatch(f["*clave"][3:], k) for k in v)
        if not isinstance(v, dict) or mala or (not v if "*" in f else set(v) != set(f)):
            return [
                f"{ruta}: {sorted(v) if isinstance(v, dict) else type(v).__name__} no cumple las claves {sorted(f)}"
            ]
        return [p for k, x in v.items() for p in shape_problems(x, f.get(k, f.get("*")), f"{ruta}.{k}")]
    if isinstance(f, list):
        repetida = isinstance(v, list) and all(isinstance(x, str) for x in v) and len(set(v)) != len(v)
        if not isinstance(v, list) or bool(f) != bool(v) or repetida:
            return [f"{ruta}: se esperaba una lista {'no vacía y sin repetidos' if f else 'vacía'}"]
        return [p for i, x in enumerate(v) for p in shape_problems(x, f[0], f"{ruta}[{i}]")]
    if isinstance(f, str) and f in _TIPOS:
        return [] if type(v) is _TIPOS[f] else [f"{ruta}: {type(v).__name__} en vez de {f}"]
    if isinstance(f, str) and f.startswith("re:"):
        return [] if isinstance(v, str) and re.fullmatch(f[3:], v) else [f"{ruta}: {v!r} no casa con {f[3:]}"]
    return [] if type(v) is type(f) and v == f else [f"{ruta}: {v!r} en vez de {f!r}"]


def _acreditar(m: dict, p: Path) -> list[str]:
    """Acredita un manifiesto YA leído contra su sello: forma, coherencia, bytes, contenido e identidad."""
    probs = shape_problems(m, ESQUEMA["manifest"], "manifiesto")
    if probs:
        return probs
    cid, sha, sucio = m["campaign_id"], m["git_sha"], m["dirty"]
    ruta = f"reports/logs/preflight_{cid}.json"
    try:
        dt.datetime.fromisoformat(m["started_at"])
    except ValueError:
        return [f"manifiesto: started_at {m['started_at']!r} no es una fecha real"]
    if m["sha"] != sha or m["preflight"] != ruta or not sha.startswith(cid.split("_")[1]):
        return ["manifiesto: sha, campaign_id y ruta del sello no describen la misma corrida"]
    sello = p.resolve().parents[2] / ruta
    if sello.is_symlink() or not sello.is_file():
        return [f"sello ausente o no regular: {ruta}"]
    crudo = sello.read_bytes()
    if hashlib.sha256(crudo).hexdigest() != m["preflight_sha256"]:
        return [f"el sello {ruta} no es el que registró el manifiesto (sha256 distinto)"]
    try:
        s = loads_strict(crudo.decode("utf-8"), finito=True)
    except (ValueError, ReceiptError) as exc:
        return [f"sello ilegible: {exc}"]
    probs = shape_problems(s, ESQUEMA["seal"], "sello")
    if not probs and (s["git"]["head"] != sha or s["git"]["dirty"] is not sucio):
        probs.append(f"sello de otra identidad: git={s['git']!r} frente a git_sha {sha} y dirty {sucio}")
    cuentas = [] if probs else [k for k in ("code", "data", "governance") if s["counts"][k] != len(s["inputs"][k])]
    return probs + [f"sello: counts.{k} no es el número de inputs.{k}" for k in cuentas]


def seal_problems(path: str | Path) -> list[str]:
    """★ M74-E-R9/R10 · la acreditación única manifiesto ↔ sello, leyendo el manifiesto una sola vez."""
    p = Path(path)
    try:
        m = loads_strict(p.read_text(encoding="utf-8"), finito=True)
    except (OSError, ValueError, ReceiptError) as exc:
        return [f"manifiesto ilegible: {exc}"]
    return _acreditar(m, p)


def publish_blocker(path: str | Path) -> str | None:
    """Motivo (str) por el que NO se puede publicar este manifiesto, o None si es publicable."""
    p = Path(path)
    if not p.exists():
        return f"falta el manifiesto de campana {p} (sin identidad sellada)"
    try:
        m = loads_strict(
            p.read_text(), finito=True
        )  # ★ M74-E-R10 · UNA lectura: `dirty` y el sello miran el mismo objeto
    except (ValueError, ReceiptError) as e:
        return f"manifiesto malformado ({type(e).__name__}: {e})"
    except OSError as e:
        return f"manifiesto ilegible ({type(e).__name__})"
    if "dirty" not in m:
        return "el manifiesto no sella la clave `dirty`"
    # `is not False` es DELIBERADO: rechaza true, "false" (string), 0 (== False pero no es
    # False), null. Solo el booleano JSON `false` autoriza publicar.
    if m["dirty"] is not False:
        return f"dirty={m['dirty']!r} (campana diagnostica) — re-lanza OFICIAL desde arbol limpio"
    problemas = _acreditar(m, p)
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
