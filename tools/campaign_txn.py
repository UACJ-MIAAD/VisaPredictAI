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

**La prohibición que hace el trabajo** es :func:`publishable`: sólo ``validated`` publica —`running`
(también el que deja un `SIGKILL`), `computed`, `failed` y `published` bloquean— y sólo con un manifiesto
OBLIGATORIO, acreditado y cruzado con la transacción. ``guard`` la consulta y ``publish`` la repite justo
antes de escribir; ninguno arranca sin ``--manifest`` (M74-E-R13). Quien escriba `campaign.json` a mano o
llame a `campaign_state` directamente sigue fuera: la envoltura no intercepta lo que no pasa por ella.

**Cómputo completo con propagación pendiente** llega a ``computed`` con ``consistency`` en
``'pending'`` (M74-B): el resultado que la campaña existe para producir —que las cifras cambien— ya
no manda la corrida a ``failed``, que es terminal. Es ``validate`` quien vuelve a exigir la
consistencia en ``'passed'``, re-ejecutando el guardián.

⚠️ **Validar es un acto humano y por eso no se teclea.** ``validate`` no acepta revisor ni decisión
por argumento: los lee de un **recibo JSON de esquema cerrado** ligado a la campaña por
``campaign_id``, SHA, hash del panel y sello de entradas. Un archivo cualquiera, un revisor vacío o una
identidad automatizada **no validan**. Y no existe bandera para saltarse el guardián de
consistencia: la que había (``--skip-consistency-check``) era un bypass de producción y se retiró.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tools import campaign_manifest as cm
from tools import campaign_state as cs

#: Ruta convenida, junto al manifiesto de campaña que ya vive ahí.
DEFAULT_PATH = Path("reports/campaign/campaign.json")
#: Raíz del repositorio que publica. Nombrada aparte para que el camino fail-closed de
#: `_run_consistency` —guardián ausente ⇒ NO se valida— sea comprobable sin mover archivos.
_REPO_ROOT = Path(__file__).resolve().parent.parent
#: Códigos de salida propios, para que un runner los distinga sin leer el texto.
EXIT_OK = 0
EXIT_ERROR = 1  #: la transición pedida es ilegal o los datos no validan
EXIT_BLOCKED = 3  #: `guard`: el estado NO autoriza publicar
EXIT_ALREADY_TERMINAL = 4  #: `fail --if-open`: ya había estado terminal, no se tocó nada


#: ─────────────────────────────────────────────────────────────── recibo de revisión humana
#: Esquema **cerrado** del recibo que `validate` exige. Antes, la revisión humana se acreditaba
#: con tres argumentos libres (`--receipt <cualquier archivo> --reviewed-by X --decision Y`): un
#: `cron-bot` podía teclearlos y la máquina no tenía forma de contradecirle. Ahora el revisor y la
#: decisión **se leen del recibo**, y el recibo está ligado a ESTA campaña.
RECEIPT_SCHEMA = "campaign-validation-receipt/2"  # /2 · M74-E-R12: + input_seal_sha256
#: ★ M74-E-R12: la revisión humana queda ligada también al sello de entradas comparado en vivo.
IDENTIDAD_RECIBO = ("campaign_id", "source_git_sha", "panel_sha256", "input_seal_sha256")
RECEIPT_KEYS = frozenset({*IDENTIDAD_RECIBO, "schema", "reviewed_by", "decision", "reviewed_at"})
#: Vocabulario cerrado: sólo `aprobada` transiciona. `rechazada` es un recibo válido cuyo desenlace
#: correcto es `fail`, no `validated` — una campaña rechazada que quedara `validated` autorizaría
#: publicar exactamente lo que el revisor acaba de rechazar.
RECEIPT_DECISIONS = ("aprobada", "rechazada")
#: Identidades que **declaran** automatización. ⚠️ Esto NO prueba humanidad y no pretende hacerlo:
#: obliga a la automatización a mentir en un artefacto firmado por su hash en vez de acreditarse
#: por omisión. Falsificar el recibo es otra clase de problema que dejar la puerta abierta.
_AUTOMATION = re.compile(
    r"(?i)(?:^|[^a-z])(?:bot|cron|ci|cd|runner|robot|automat\w*|github[-_ ]?actions?|jenkins|"
    r"pipeline|daemon|service[-_ ]?account|no[-_]?reply|system|agent|script)(?:[^a-z]|$)"
)
#: Un nombre de persona no cabe en dos caracteres; y un revisor de una sola palabra genérica
#: («admin») no identifica a nadie que pueda responder de la decisión.
_MIN_REVISOR = 4


class ReceiptError(ValueError):
    """El recibo de revisión no acredita una validación humana de ESTA campaña."""


def load_validation_receipt(path: str | Path, estado: Mapping[str, Any]) -> dict[str, str]:
    """Lee y **acredita** el recibo contra la campaña abierta. Fail-closed en todo lo demás.

    Devuelve el recibo ya validado. Lanza :class:`ReceiptError` nombrando exactamente qué falló,
    para que el operador no tenga que adivinar cuál de los ocho campos no cuadra.
    """
    ruta = Path(path)
    if not ruta.is_file():
        raise ReceiptError(f"el recibo {ruta} no existe")
    try:
        #: `cs.loads` rechaza claves duplicadas: nadie esconde un segundo `decision`.
        datos = cs.loads(ruta.read_text(encoding="utf-8"))
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"recibo ilegible, no es un objeto o tiene claves duplicadas: {exc}") from exc

    faltan = sorted(RECEIPT_KEYS - set(datos))
    sobran = sorted(set(datos) - RECEIPT_KEYS)
    if faltan or sobran:
        raise ReceiptError(f"esquema del recibo: faltan {faltan}, sobran {sobran}")
    for clave, valor in sorted(datos.items()):
        if not isinstance(valor, str) or not valor.strip():
            raise ReceiptError(f"el campo {clave!r} del recibo debe ser una cadena no vacía")
    if datos["schema"] != RECEIPT_SCHEMA:
        raise ReceiptError(f"esquema {datos['schema']!r}; se esperaba {RECEIPT_SCHEMA!r}")

    # ── ligado a ESTA campaña: cuatro identidades, no una
    for clave in IDENTIDAD_RECIBO:
        if datos[clave] != estado.get(clave):
            raise ReceiptError(
                f"el recibo dice {clave}={datos[clave]!r} y la campaña abierta tiene "
                f"{estado.get(clave)!r}: no revisan la misma corrida"
            )

    # ── la decisión, del vocabulario cerrado
    if datos["decision"] not in RECEIPT_DECISIONS:
        raise ReceiptError(f"decision {datos['decision']!r} fuera del vocabulario {RECEIPT_DECISIONS}")
    if datos["decision"] != "aprobada":
        raise ReceiptError(
            "el recibo RECHAZA la campaña: el desenlace correcto es `fail`, no `validated` "
            "(validarla autorizaría publicar justo lo que la revisión rechaza)"
        )

    # ── el revisor: una persona que pueda responder de la decisión
    revisor = datos["reviewed_by"].strip()
    if len(revisor) < _MIN_REVISOR:
        raise ReceiptError(f"reviewed_by {revisor!r} es demasiado corto para identificar a nadie")
    if _AUTOMATION.search(revisor):
        raise ReceiptError(
            f"reviewed_by {revisor!r} declara una identidad automatizada; la validación de una "
            "campaña es un acto humano y responde una persona"
        )

    # ── la fecha: una revisión no puede preceder a lo que revisa
    if not cs._valid_ts(datos["reviewed_at"]):
        raise ReceiptError(f"reviewed_at {datos['reviewed_at']!r} no es una marca RFC3339 válida")
    piso_clave = "completed_at" if estado.get("completed_at") else "started_at"
    piso = estado.get(piso_clave)
    if isinstance(piso, str) and _parse_ts(datos["reviewed_at"]) < _parse_ts(piso):
        raise ReceiptError(
            f"reviewed_at ({datos['reviewed_at']}) precede a {piso_clave} ({piso}): "
            "un recibo escrito antes del cómputo no puede ser su revisión"
        )
    return {k: str(v) for k, v in datos.items()}


def _parse_ts(valor: str) -> datetime:
    return datetime.fromisoformat(valor.replace("Z", "+00:00"))


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
    input_seal_sha256: str,
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
        input_seal_sha256=input_seal_sha256,
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


def publishable(path: str | Path, *, manifest: str | Path) -> tuple[bool, str]:
    """¿Autoriza este estado a publicar? Sólo ``validated``, y sólo si TODO lo acredita.

    Comprueba, además del estado: esquema íntegro (que desde M74-B exige gates con valor, revisión
    ≥ 1, cadenas no vacías y claves compatibles), **árbol limpio** (``git_dirty=False``: la ruta
    diagnóstica `ALLOW_DIRTY` llegaba a `validated` y publicaba), el **recibo de validación por
    ruta, existencia, hash y acreditación completa** —esquema cerrado, ligado a ESTA campaña, con
    revisor y decisión que coinciden con los que el estado declara— y el **manifiesto, OBLIGATORIO**
    (M74-E-R13): publicable por sí mismo y con campaña, SHA, dirty y sello iguales a los de la transacción.
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
    ok_recibo, motivo = _receipt_matches(actual)
    if not ok_recibo:
        return False, motivo
    ok_id, motivo = _manifest_matches(manifest, actual)
    if not ok_id:
        return False, motivo
    return True, f"campaña {actual['campaign_id']} en '{estado}': autoriza publicar"


def _receipt_matches(obj: Mapping[str, Any]) -> tuple[bool, str]:
    """El recibo debe existir, conservar su hash **y volver a acreditarse** contra la campaña.

    ⚠️ Hasta M74-B-R1 esto sólo comprobaba ruta, existencia y hash, y ahí estaba el agujero: la
    acreditación del recibo vivía en `validate`, es decir en el camino de ESCRITURA. Un
    `campaign.json` en `validated` escrito a mano —o por cualquier vía que no pase por esta CLI—
    publicaba con **cualquier archivo** cuya ruta y hash cuadraran: un markdown de texto libre, o un
    recibo válido pero **de otra campaña**. La propia suite lo demostraba sin saberlo.

    Es la misma lección que M73 dejó escrita sobre las invariantes de estado y que aquí no se aplicó
    al recibo: **lo que decide publicar es un LECTOR, así que la acreditación tiene que vivir en la
    lectura**, no sólo en la escritura.
    """
    rel = obj.get("validation_receipt_path")
    esperado = obj.get("validation_receipt_sha256")
    if not rel or not esperado:
        return False, "la transacción no liga un recibo de validación (ruta + sha256)"
    recibo = Path(rel)
    if not recibo.is_absolute():
        # ⚠️ Antes se resolvía subiendo tres niveles desde `campaign.json`, así que mover el estado
        # cambiaba QUÉ archivo se verificaba. `validate` guarda ya la ruta absoluta; una relativa
        # sólo puede venir de un estado escrito a mano, y adivinar su raíz sería fail-open.
        return False, f"el recibo se liga por una ruta relativa ({rel}) y no puede verificarse sin ambigüedad"
    if not recibo.is_file():
        return False, f"el recibo de validación no existe: {rel}"
    real = sha256_file(recibo)
    if real != esperado:
        return False, f"el recibo {rel} cambió desde la validación (sha {real[:12]}… ≠ {str(esperado)[:12]}…)"
    # ★ R2: se vuelve a ACREDITAR, no sólo a comparar el hash. Esquema cerrado, ligado a esta
    # campaña por campaign_id + SHA de origen + panel_sha256, decisión del vocabulario, revisor no
    # automatizado y fecha posterior al cómputo.
    try:
        acta = load_validation_receipt(recibo, obj)
    except ReceiptError as exc:
        return False, f"el recibo ligado NO acredita esta validación: {exc}"
    # ★ y el estado no puede contar una historia distinta de la del recibo que dice sostenerlo
    for clave in ("reviewed_by", "decision"):
        if acta[clave] != obj.get(clave):
            return False, (
                f"el estado dice {clave}={obj.get(clave)!r} y el recibo {acta[clave]!r}: "
                "la transacción no describe la revisión que liga"
            )
    return True, ""


#: ★ M74-E-R12 · manifiesto ↔ transacción: campaña, SHA, `dirty` y el sello comparado en vivo (antes, sólo el id).
_MANIFIESTO_VS_TXN = {"campaign_id": "campaign_id", "git_sha": "source_git_sha", "dirty": "git_dirty"}
_MANIFIESTO_VS_TXN["preflight_sha256"] = "input_seal_sha256"


def _manifest_matches(manifest: str | Path, obj: Mapping[str, Any]) -> tuple[bool, str]:
    """La MISMA corrida campo a campo y con su tipo (H15), y un manifiesto publicable por sí mismo sobre la
    MISMA lectura (M74-E-R13): uno que copiaba `preflight_sha256` de la transacción pasaba con un sello falso."""
    datos, bloqueo = cm.leer_para_publicar(manifest)
    if datos is None:
        return False, f"manifiesto de campaña no publicable: {bloqueo}"
    for m, t in _MANIFIESTO_VS_TXN.items():
        if datos.get(m) != obj.get(t) or type(datos.get(m)) is not type(obj.get(t)):
            return False, f"manifiesto {m}={datos.get(m)!r} ≠ transacción {t}={obj.get(t)!r}: no son la misma corrida"
    return (False, f"manifiesto de campaña no publicable: {bloqueo}") if bloqueo else (True, "")


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
    o.add_argument("--input-seal", required=True, help="sha256 del sello de entradas comparado en vivo al arrancar")

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
    v.add_argument(
        "--receipt",
        required=True,
        help=(
            f"recibo JSON de esquema cerrado ({RECEIPT_SCHEMA}) con las ocho claves "
            f"{sorted(RECEIPT_KEYS)}. El revisor y la decisión se LEEN de ahí y el recibo se liga a "
            "la campaña por campaign_id, SHA, hash del panel y sello de entradas. No hay forma de saltarse "
            "el guardián de consistencia."
        ),
    )

    pu = sub.add_parser("publish", help="validated -> published (sólo el publicador)")
    pu.add_argument("--release-sha", required=True)
    pu.add_argument("--manifest", required=True, help="manifiesto de campaña: se re-acredita justo antes de publicar")

    a = sub.add_parser("archive", help="aparta una transacción YA terminal para la siguiente campaña")
    a.add_argument("--dir", default="reports/campaign/transactions", help="carpeta destino")

    g = sub.add_parser("guard", help="sale != 0 salvo que el estado autorice publicar")
    g.add_argument("--manifest", required=True, help="manifiesto de campaña: se acredita y se cruza con la transacción")

    sub.add_parser("status", help="imprime el estado actual")
    return p


def _run_consistency() -> tuple[bool, str]:
    """Re-ejecuta el guardián de consistencia del repositorio. Fail-closed si no se puede.

    ⚠️ La raíz sale de la ubicación de **este módulo**, no de dónde viva `campaign.json`. Antes se
    derivaba subiendo tres niveles desde la transacción, así que mover el archivo —o apuntarlo a un
    temporal— cambiaba qué guardián se ejecutaba, o hacía que no se encontrara ninguno. El guardián
    pertenece al repositorio que publica, no al directorio del estado.
    """
    guardian = _REPO_ROOT / "tools" / "check_consistency.py"
    if not guardian.is_file():
        return False, f"no se encontró {guardian}"
    fin = subprocess.run(
        [sys.executable, str(guardian)], cwd=_REPO_ROOT, capture_output=True, text=True, check=False, timeout=1800
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
                input_seal_sha256=args.input_seal,
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
            ok_cons, salida = _run_consistency()
            if not ok_cons:
                print(f"✗ la consistencia NO está acreditada; propaga y reintenta:\n{salida}", file=sys.stderr)
                return EXIT_ERROR
            abierta = cs.read(path)
            if abierta is None:
                print(f"✗ no hay transacción legible en {path}", file=sys.stderr)
                return EXIT_ERROR
            recibo = Path(args.receipt).resolve()
            acta = load_validation_receipt(recibo, abierta)
            obj = cs.mark_validated(
                path,
                validation_receipt_sha256=sha256_file(recibo),
                #: ★ ABSOLUTA: antes se guardaba tal cual se tecleó y `publishable` tenía que
                #: adivinar la raíz contra la que resolverla. Mover `campaign.json` cambiaba qué
                #: archivo se verificaba.
                validation_receipt_path=str(recibo),
                reviewed_by=acta["reviewed_by"],
                validated_at=now_rfc3339(),
                decision=acta["decision"],
            )
            print(f"✓ campaña {obj['campaign_id']} → 'validated' por {obj['reviewed_by']} ({acta['reviewed_at']})")
        elif args.cmd in ("guard", "publish"):
            # ★ M74-E-R13 · `publish` repite la MISMA puerta que `guard` inmediatamente antes de consumir el permiso
            ok, motivo = publishable(path, manifest=args.manifest)
            if not ok or args.cmd == "guard":
                print(("✓ " if ok else "✗ PUBLICACIÓN BLOQUEADA: ") + motivo, file=sys.stdout if ok else sys.stderr)
                return EXIT_OK if ok else EXIT_BLOCKED
            obj = cs.mark_published(path, published_at=now_rfc3339(), release_sha=args.release_sha)
            print(f"✓ campaña {obj['campaign_id']} → 'published' ({obj['release_sha']})")
        elif args.cmd == "archive":
            final = archive_if_terminal(path, args.dir)
            print(f"· sin transacción previa en {path}" if final is None else f"✓ transacción archivada → {final}")
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
