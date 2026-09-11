"""Conduce la transacción de campaña de ADR 0003 desde un runner. Envoltura reutilizable.

`tools/campaign_state.py` implementa la máquina de estados desde julio, pero **ningún runner la
conducía** (pendiente #56): las campañas se llevaban a mano y `campaign.json` no se escribía. Este
módulo es la pieza que faltaba, y es **una sola** para los dos tipos de consumidor:

* **Desde bash** —los runners del proyecto son `.sh`— por subcomandos:
  ``open`` · ``compute`` · ``fail`` · ``validate`` · ``publish`` · ``guard`` · ``status``.
* **Desde Python** por el gestor de contexto :func:`campaign`, que sella al entrar y marca
  ``failed`` si el cuerpo lanza — incluida una interrupción.

Sin esto, cada runner habría acabado con su propia secuencia de llamadas y su propio criterio de
qué hacer ante un estado terminal: exactamente la duplicación que el proyecto lleva meses
cerrando.

**La prohibición que hace el trabajo** es ``guard``: sale distinto de cero salvo que el estado sea
``validated``. `running` (incluido el que deja un `SIGKILL`), `computed`, `failed` y `published`
bloquean. Un publicador que no llame a ``guard`` no está protegido: la envoltura no puede
interceptar lo que no pasa por ella.

⚠️ **Límite declarado, heredado de la máquina:** el enum de ADR 0003 no tiene un estado para
«técnicamente completa pero con cifras por propagar». ``mark_computed`` exige los tres gates en
``passed``, así que una campaña cuya consistencia queda rota **no puede** llegar a ``computed``.
Este envoltorio NO inventa un estado: el runner la marca ``failed`` con una razón que dice
literalmente que el cómputo terminó y lo que falta es propagar. El nombre del estado es impreciso;
la razón es exacta, y el efecto —no se publica— es el correcto. Alternativa descartada: dejarla en
``running``, que la volvería indistinguible de una campaña muerta a mitad.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from tools import campaign_state as cs

#: Ruta convenida, junto al manifiesto de campaña que ya vive ahí.
DEFAULT_PATH = Path("reports/campaign/campaign.json")
#: Códigos de salida propios, para que un runner los distinga sin leer el texto.
EXIT_OK = 0
EXIT_ERROR = 1  #: la transición pedida es ilegal o los datos no validan
EXIT_BLOCKED = 3  #: `guard`: el estado NO autoriza publicar
EXIT_ALREADY_TERMINAL = 4  #: `fail --if-open`: ya había estado terminal, no se tocó nada


def now_rfc3339() -> str:
    """Marca de tiempo UTC con zona, en el formato que el esquema exige."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: str | Path) -> str:
    """sha256 hexadecimal del contenido de un archivo, leído por bloques."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for bloque in iter(lambda: fh.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def panel_fingerprint(path: str | Path) -> str:
    """Huella del panel en la forma que pide el esquema (``sha256:`` + 64 hex)."""
    return "sha256:" + sha256_file(path)


def open_campaign(
    path: str | Path,
    *,
    campaign_id: str,
    source_git_sha: str,
    git_dirty: bool,
    panel: str | Path,
    started_at: str | None = None,
) -> dict:
    """Sella la campaña en ``running``. Create-only: si ya existe, aborta (no se reinicia)."""
    return cs.seal_running(
        path,
        campaign_id=campaign_id,
        source_git_sha=source_git_sha,
        git_dirty=git_dirty,
        panel_sha256=panel_fingerprint(panel),
        started_at=started_at or now_rfc3339(),
    )


def fail_if_open(path: str | Path, *, stage: str, reason: str, exit_code: int | None = None) -> dict | None:
    """Marca ``failed`` **sólo** si la campaña sigue abierta; si ya es terminal, no toca nada.

    Un runner de shell instala esto en un ``trap``, que se dispara **también** después de un fallo
    ya registrado. Sin esta guarda, el trap intentaría `failed -> failed`, la máquina lo rechazaría
    con razón, y el runner moriría con un error espurio que taparía el fallo verdadero.
    """
    actual = cs.read(path)
    if actual is None:
        return None
    if actual["status"] in cs.TERMINAL:
        return None
    return cs.mark_failed(path, failed_stage=stage, failed_at=now_rfc3339(), reason=reason, exit_code=exit_code)


def publishable(path: str | Path) -> tuple[bool, str]:
    """¿Autoriza este estado a publicar? Sólo ``validated``. Fail-closed ante ausencia."""
    if not Path(path).exists():
        return False, f"no hay transacción de campaña en {path} — publicar exige una campaña validada"
    actual = cs.read(path)
    if actual is None:
        return False, f"{path} ilegible o no es un objeto JSON"
    problems = cs.validate_schema(actual)
    if problems:
        return False, f"{path} no cumple el esquema: {problems}"
    estado = actual["status"]
    if estado != cs.PUBLISHABLE:
        return False, f"estado '{estado}' NO autoriza publicar (sólo '{cs.PUBLISHABLE}')"
    return True, f"campaña {actual['campaign_id']} en '{estado}': autoriza publicar"


def archive_if_terminal(path: str | Path, destino: str | Path) -> Path | None:
    """Aparta una transacción **ya terminal** para dejar sitio a la siguiente campaña.

    ``seal_running`` es create-only a propósito: una campaña no se reinicia encima de otra. Pero un
    runbook se relanza, así que alguien tiene que archivar la anterior, y hacerlo a mano es
    justamente donde se pierde la procedencia. Aquí se mueve con su ``campaign_id`` por nombre, y
    **sólo** si terminó: una campaña en ``running``/``computed``/``validated`` NO se archiva —
    lanzar otra encima de una abierta es el error que la máquina existe para impedir.
    """
    origen = Path(path)
    if not origen.exists():
        return None
    actual = cs.read(origen)
    if actual is None:
        raise ValueError(f"{origen} existe pero es ilegible: archívala o bórrala a mano")
    if actual["status"] not in cs.TERMINAL:
        raise ValueError(
            f"{origen} sigue ABIERTA en '{actual['status']}' ({actual['campaign_id']}): "
            "ciérrala (fail/publish) antes de lanzar otra campaña"
        )
    carpeta = Path(destino)
    carpeta.mkdir(parents=True, exist_ok=True)
    final = carpeta / f"{actual['campaign_id']}.json"
    if final.exists():
        raise ValueError(f"ya hay una transacción archivada en {final}: dos campañas con el mismo id")
    os.replace(origen, final)
    return final


@contextmanager
def campaign(path: str | Path, **sello) -> Iterator[dict]:
    """Sella al entrar y marca ``failed`` si el cuerpo lanza. Para runners escritos en Python.

    No marca ``computed`` al salir bien: llegar a ``computed`` exige los tres gates en `passed`, y
    eso lo sabe el runner, no este gestor. Salir sin excepción deja la campaña en ``running``, que
    bloquea publicar: el silencio nunca autoriza.
    """
    estado = open_campaign(path, **sello)
    completo = False
    try:
        yield estado
        completo = True
    finally:
        # `finally` en vez de `except BaseException`: cubre MÁS —también `KeyboardInterrupt`,
        # `SystemExit` y un `GeneratorExit`— sin capturar nada ni alterar la propagación, y no
        # añade una captura amplia al trinquete de deuda. La causa se lee de la excepción en
        # vuelo, así que la razón registrada sigue nombrando qué reventó.
        if not completo:
            exc = sys.exception()
            causa = f"{type(exc).__name__}: {exc}" if exc is not None else "el cuerpo no terminó"
            fail_if_open(path, stage="cuerpo de la campaña", reason=causa)


# ------------------------------------------------------------------ interfaz de línea de órdenes
def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="campaign_txn", description=__doc__.split("\n")[0])
    p.add_argument("--path", default=str(DEFAULT_PATH), help=f"ruta de campaign.json (def. {DEFAULT_PATH})")
    sub = p.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("open", help="sella la campaña en running (create-only)")
    o.add_argument("--campaign-id", required=True)
    o.add_argument("--sha", required=True, help="SHA de git de 40 hex sellado para toda la corrida")
    o.add_argument("--dirty", required=True, choices=("true", "false"), help="literal exacto; no se coerciona")
    o.add_argument("--panel", required=True, help="ruta del panel del que se deriva panel_sha256")

    c = sub.add_parser("compute", help="running -> computed (exige los tres gates en passed)")
    for gate in ("input-gate", "output-gate", "consistency"):
        c.add_argument(f"--{gate}", required=True)

    f = sub.add_parser("fail", help="-> failed (terminal)")
    f.add_argument("--stage", required=True)
    f.add_argument("--reason", required=True)
    f.add_argument("--exit-code", type=int, default=None)
    f.add_argument(
        "--if-open",
        action="store_true",
        help="no hacer nada si la campaña ya es terminal (para traps de shell)",
    )

    v = sub.add_parser("validate", help="computed -> validated (revisión humana)")
    v.add_argument("--receipt", required=True, help="archivo del recibo; su sha256 se deriva, no se teclea")
    v.add_argument("--reviewed-by", required=True)
    v.add_argument("--decision", required=True)

    pu = sub.add_parser("publish", help="validated -> published (sólo el publicador)")
    pu.add_argument("--release-sha", required=True)

    a = sub.add_parser("archive", help="aparta una transacción YA terminal para la siguiente campaña")
    a.add_argument("--dir", default="reports/campaign/transactions", help="carpeta destino")

    sub.add_parser("guard", help="sale != 0 salvo que el estado autorice publicar")
    sub.add_parser("status", help="imprime el estado actual")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    path = Path(args.path)
    try:
        if args.cmd == "open":
            obj = open_campaign(
                path,
                campaign_id=args.campaign_id,
                source_git_sha=args.sha,
                git_dirty=args.dirty == "true",
                panel=args.panel,
            )
            print(f"✓ campaña {obj['campaign_id']} sellada en 'running' → {path}")
        elif args.cmd == "compute":
            obj = cs.mark_computed(
                path,
                completed_at=now_rfc3339(),
                input_gate=args.input_gate,
                output_gate=args.output_gate,
                consistency=args.consistency,
            )
            print(f"✓ campaña {obj['campaign_id']} → 'computed' (revision {obj['revision']})")
        elif args.cmd == "fail":
            if args.if_open:
                quizas = fail_if_open(path, stage=args.stage, reason=args.reason, exit_code=args.exit_code)
                if quizas is None:
                    actual = cs.read(path)
                    estado = actual["status"] if actual else "ausente"
                    print(f"· campaña ya terminal o ausente ('{estado}'): no se toca")
                    return EXIT_ALREADY_TERMINAL
                obj = quizas
            else:
                obj = cs.mark_failed(
                    path,
                    failed_stage=args.stage,
                    failed_at=now_rfc3339(),
                    reason=args.reason,
                    exit_code=args.exit_code,
                )
            print(f"✗ campaña {obj['campaign_id']} → 'failed' en {args.stage}: {args.reason}")
        elif args.cmd == "validate":
            obj = cs.mark_validated(
                path,
                validation_receipt_sha256=sha256_file(args.receipt),
                reviewed_by=args.reviewed_by,
                validated_at=now_rfc3339(),
                decision=args.decision,
            )
            print(f"✓ campaña {obj['campaign_id']} → 'validated' por {obj['reviewed_by']}")
        elif args.cmd == "publish":
            obj = cs.mark_published(path, published_at=now_rfc3339(), release_sha=args.release_sha)
            print(f"✓ campaña {obj['campaign_id']} → 'published' ({obj['release_sha']})")
        elif args.cmd == "archive":
            final = archive_if_terminal(path, args.dir)
            print(f"· sin transacción previa en {path}" if final is None else f"✓ transacción archivada → {final}")
        elif args.cmd == "guard":
            ok, motivo = publishable(path)
            print(("✓ " if ok else "✗ PUBLICACIÓN BLOQUEADA: ") + motivo, file=sys.stdout if ok else sys.stderr)
            return EXIT_OK if ok else EXIT_BLOCKED
        elif args.cmd == "status":
            actual = cs.read(path)
            print(json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=True) if actual else "ausente")
    except (ValueError, OSError) as exc:
        print(f"✗ transacción de campaña: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


if __name__ == "__main__":
    os.environ.setdefault("PYTHONHASHSEED", "0")
    raise SystemExit(main())
