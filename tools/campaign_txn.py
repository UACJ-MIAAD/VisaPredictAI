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
import subprocess
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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


def publishable(path: str | Path, *, manifest: str | Path | None = None) -> tuple[bool, str]:
    """¿Autoriza este estado a publicar? Sólo ``validated``, y sólo si TODO lo acredita.

    Comprueba, además del estado: esquema íntegro (que desde M74-B exige gates con valor, revisión
    ≥ 1, cadenas no vacías y claves compatibles), **árbol limpio** (``git_dirty=False``: la ruta
    diagnóstica `ALLOW_DIRTY` llegaba a `validated` y publicaba), el **recibo de validación por
    ruta, existencia y hash** (un sha suelto no acredita nada si el archivo no está) y, si se
    pasa el manifiesto de campaña, que su ``campaign_id`` **coincida** con el de la transacción.
    """
    ruta = Path(path)
    if not ruta.exists():
        return False, f"no hay transacción de campaña en {ruta} — publicar exige una campaña validada"
    actual = cs.read(ruta)
    if actual is None:
        return False, f"{ruta} ilegible o no es un objeto JSON"
    problems = cs.validate_schema(actual)
    if problems:
        return False, f"{ruta} no cumple el esquema: {problems}"
    estado = actual["status"]
    if estado != cs.PUBLISHABLE:
        return False, f"estado '{estado}' NO autoriza publicar (sólo '{cs.PUBLISHABLE}')"
    if actual.get("git_dirty") is not False:
        return False, "la campaña se selló con el árbol SUCIO (git_dirty): es diagnóstica y no se publica"
    ok_recibo, motivo = _receipt_matches(ruta, actual)
    if not ok_recibo:
        return False, motivo
    if manifest is not None:
        ok_id, motivo = _manifest_matches(manifest, actual)
        if not ok_id:
            return False, motivo
    return True, f"campaña {actual['campaign_id']} en '{estado}': autoriza publicar"


def _receipt_matches(txn_path: Path, obj: Mapping[str, Any]) -> tuple[bool, str]:
    """El recibo de validación debe EXISTIR y seguir teniendo el hash que se validó."""
    rel = obj.get("validation_receipt_path")
    esperado = obj.get("validation_receipt_sha256")
    if not rel or not esperado:
        return False, "la transacción no liga un recibo de validación (ruta + sha256)"
    recibo = Path(rel)
    if not recibo.is_absolute():
        recibo = txn_path.resolve().parent.parent.parent / rel
    if not recibo.is_file():
        return False, f"el recibo de validación no existe: {rel}"
    real = sha256_file(recibo)
    if real != esperado:
        return False, f"el recibo {rel} cambió desde la validación (sha {real[:12]}… ≠ {str(esperado)[:12]}…)"
    return True, ""


def _manifest_matches(manifest: str | Path, obj: Mapping[str, Any]) -> tuple[bool, str]:
    """El manifiesto de campaña y la transacción deben hablar de la MISMA campaña.

    Antes cada uno acreditaba por su lado y dos `campaign_id` distintos pasaban ambos gates (H15).
    """
    ruta = Path(manifest)
    if not ruta.is_file():
        return False, f"no existe el manifiesto de campaña {ruta}"
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return False, f"manifiesto de campaña ilegible ({exc})"
    if datos.get("campaign_id") != obj.get("campaign_id"):
        return False, (
            f"el manifiesto describe la campaña {datos.get('campaign_id')!r} y la transacción "
            f"{obj.get('campaign_id')!r}: no son la misma corrida"
        )
    return True, ""


def archive_if_terminal(path: str | Path, destino: str | Path) -> Path | None:
    """Aparta una transacción **ya terminal** para dejar sitio a la siguiente campaña.

    ``seal_running`` es create-only a propósito: una campaña no se reinicia encima de otra. Pero un
    runbook se relanza, así que alguien tiene que archivar la anterior, y hacerlo a mano es
    justamente donde se pierde la procedencia. Aquí se mueve con su ``campaign_id`` por nombre, y
    **sólo** si terminó: una campaña en ``running``/``computed``/``validated`` NO se archiva —
    lanzar otra encima de una abierta es el error que la máquina existe para impedir.

    ⚠️ Valida el esquema antes de decidir (H23): un `campaign.json` corrupto no puede archivarse
    como si se entendiera, ni tratarse como «no hay ninguna».
    """
    origen = Path(path)
    if not origen.exists():
        return None
    actual = cs.read(origen)
    if actual is None:
        raise ValueError(f"{origen} existe pero es ilegible: archívala o bórrala a mano")
    problemas = cs.validate_schema(actual)
    if problemas:
        raise ValueError(f"{origen} no cumple el esquema y no se archiva a ciegas: {problemas}")
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

    c = sub.add_parser(
        "compute", help="running -> computed (input/output en passed; consistency puede quedar pendiente)"
    )
    for gate in ("input-gate", "output-gate", "consistency"):
        c.add_argument(f"--{gate}", required=True)
    c.add_argument(
        "--best-effort-failure",
        action="append",
        default=[],
        dest="best_effort",
        help="etapa tolerable que falló; repetible. Queda en el recibo para la revisión humana",
    )

    f = sub.add_parser("fail", help="-> failed (terminal)")
    f.add_argument("--stage", required=True)
    f.add_argument("--reason", required=True)
    f.add_argument("--exit-code", type=int, default=None)
    f.add_argument(
        "--if-open",
        action="store_true",
        help="no hacer nada si la campaña ya es terminal (para traps de shell)",
    )

    v = sub.add_parser("validate", help="computed -> validated (revisión humana + consistencia acreditada)")
    v.add_argument("--receipt", required=True, help="archivo del recibo; su sha256 se deriva, no se teclea")
    v.add_argument("--reviewed-by", required=True)
    v.add_argument("--decision", required=True)
    v.add_argument(
        "--skip-consistency-check",
        action="store_true",
        help="NO recomendado: omite la re-ejecución de tools/check_consistency.py (debe justificarse)",
    )

    pu = sub.add_parser("publish", help="validated -> published (sólo el publicador)")
    pu.add_argument("--release-sha", required=True)

    a = sub.add_parser("archive", help="aparta una transacción YA terminal para la siguiente campaña")
    a.add_argument("--dir", default="reports/campaign/transactions", help="carpeta destino")

    g = sub.add_parser("guard", help="sale != 0 salvo que el estado autorice publicar")
    g.add_argument("--manifest", default=None, help="manifiesto de campaña, para cruzar su campaign_id")

    sub.add_parser("status", help="imprime el estado actual")
    return p


def _run_consistency(txn_path: Path) -> tuple[bool, str]:
    """Re-ejecuta el guardián de consistencia del repositorio. Fail-closed si no se puede."""
    raiz = txn_path.resolve().parent.parent.parent
    guardian = raiz / "tools" / "check_consistency.py"
    if not guardian.is_file():
        return False, f"no se encontró {guardian}"
    fin = subprocess.run(
        [sys.executable, str(guardian)], cwd=raiz, capture_output=True, text=True, check=False, timeout=1800
    )
    return fin.returncode == 0, (fin.stdout or "") + (fin.stderr or "")


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
                best_effort_failures=args.best_effort or None,
            )
            nota = "" if obj["consistency"] == cs._GATE_OK else " — consistencia PENDIENTE de propagar"
            print(f"✓ campaña {obj['campaign_id']} → 'computed' (revision {obj['revision']}){nota}")
            if obj.get("best_effort_failures"):
                print(
                    f"  ⚠️ etapas tolerables fallidas ({len(obj['best_effort_failures'])}): "
                    f"{', '.join(obj['best_effort_failures'])}"
                )
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
            # ★ H2: validar EXIGE la consistencia acreditada. Si el cómputo la dejó en `pending`
            # porque las cifras cambiaron, aquí se vuelve a ejecutar el guardián: propagar es la
            # precondición de validar, no un paso de buena fe.
            if not args.skip_consistency_check:
                ok_cons, salida = _run_consistency(path)
                if not ok_cons:
                    print(f"✗ la consistencia NO está acreditada; propaga y reintenta:\n{salida}", file=sys.stderr)
                    return EXIT_ERROR
            recibo = Path(args.receipt)
            if not recibo.is_file():
                print(f"✗ el recibo {recibo} no existe", file=sys.stderr)
                return EXIT_ERROR
            obj = cs.mark_validated(
                path,
                validation_receipt_sha256=sha256_file(recibo),
                validation_receipt_path=str(recibo),
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
            ok, motivo = publishable(path, manifest=args.manifest)
            print(("✓ " if ok else "✗ PUBLICACIÓN BLOQUEADA: ") + motivo, file=sys.stdout if ok else sys.stderr)
            return EXIT_OK if ok else EXIT_BLOCKED
        elif args.cmd == "status":
            actual = cs.read(path)
            if actual is None:
                # ⚠️ Distinguir «no hay» de «hay y no se entiende» (H23): antes ambos decían «ausente».
                print("ausente" if not path.exists() else f"ILEGIBLE o con claves duplicadas: {path}")
                return EXIT_OK if not path.exists() else EXIT_ERROR
            problemas = cs.validate_schema(actual)
            print(json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=True))
            if problemas:
                print(f"✗ esquema inválido: {problemas}", file=sys.stderr)
                return EXIT_ERROR
    except (ValueError, OSError) as exc:
        print(f"✗ transacción de campaña: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


if __name__ == "__main__":
    os.environ.setdefault("PYTHONHASHSEED", "0")
    raise SystemExit(main())
