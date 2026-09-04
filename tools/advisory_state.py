#!/usr/bin/env python3
"""D2-B: estado visible de los advisories del cron (campeón-retador y añada sombra).

Los dos pasos advisory (``experiments/run_champion_challenger.py`` y
``experiments/freeze_shadow.py``) NUNCA bloquean release ni deploy: su fallo se degrada a
un marcador. El problema que cierra este módulo es que un fallo crónico quedaba invisible
salvo leyendo una línea suelta del correo. Aquí:

- se leen los marcadores del run (valor EXACTO ``ok`` o ``failed``; cualquier otra cosa es
  inválida y se trata como fallo de contrato, no como ``ok``);
- se lleva un contador de fallos CONSECUTIVOS por advisory en
  ``reports/governance/advisory_state.json`` (schema cerrado, escritura atómica);
- a los dos fallos consecutivos de cualquiera de los dos se abre UN issue con label
  estable; el primer fallo no abre nada y las corridas siguientes no duplican;
- la recuperación total (ambos en ``ok``) pide comentario único y cierre.

El archivo de estado se crea en una corrida REAL con rebuild; su ausencia inicializa. Un
estado corrupto NO se sobrescribe: se conserva, se avisa y se hace visible en el correo.

Uso desde el workflow (todo no bloqueante):

    python tools/advisory_state.py update \
        --state reports/governance/advisory_state.json \
        --marker champion_challenger=/tmp/champion_status.txt \
        --marker shadow_freeze=/tmp/shadow_status.txt \
        --run-id "$GITHUB_RUN_ID" --out /tmp/advisory_summary.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
#: Nombres FIJOS de los dos advisories; el schema no admite otros.
ADVISORIES: tuple[str, ...] = ("champion_challenger", "shadow_freeze")
#: Valores EXACTOS admitidos en un marcador.
OK, FAILED = "ok", "failed"
STATUSES: tuple[str, ...] = (OK, FAILED)
#: Umbral de consecutividad que abre el issue (el primer fallo no abre nada).
ISSUE_THRESHOLD = 2
#: Label estable del issue único.
ISSUE_LABEL = "mlops-advisory"
ISSUE_TITLE = "MLOps advisories del cron fallando de forma consecutiva"


class CorruptStateError(Exception):
    """El archivo de estado existe pero no cumple el schema cerrado."""


def marker_status(raw: str | None) -> str:
    """Traduce el contenido de un marcador a ``ok``/``failed`` (estricto).

    Solo el valor exacto ``ok`` (sin espacios alrededor tras ``strip``) cuenta como éxito.
    Ausente, vacío, prosa libre o cualquier otro valor cuenta como ``failed``: N sale de los
    marcadores, jamás de texto libre.
    """
    if raw is None:
        return FAILED
    value = raw.strip()
    return OK if value == OK else FAILED


def read_marker(path: Path) -> str:
    try:
        return marker_status(path.read_text(encoding="utf-8"))
    except OSError:
        return FAILED


def _validate(state: Any) -> dict[str, Any]:
    """Valida el schema CERRADO; lanza ``CorruptStateError`` si algo no cuadra."""
    if not isinstance(state, dict):
        raise CorruptStateError(f"la raíz no es un objeto ({type(state).__name__})")
    expected_keys = {"schema_version", "updated_at", "run_id", "advisories"}
    if set(state) != expected_keys:
        raise CorruptStateError(f"claves {sorted(state)} != {sorted(expected_keys)}")
    if state["schema_version"] != SCHEMA_VERSION or isinstance(state["schema_version"], bool):
        raise CorruptStateError(f"schema_version {state['schema_version']!r} != {SCHEMA_VERSION}")
    for key in ("updated_at", "run_id"):
        if not isinstance(state[key], str) or not state[key]:
            raise CorruptStateError(f"{key} debe ser una cadena no vacía")
    adv = state["advisories"]
    if not isinstance(adv, dict) or set(adv) != set(ADVISORIES):
        raise CorruptStateError(f"advisories {sorted(adv) if isinstance(adv, dict) else adv!r} != {sorted(ADVISORIES)}")
    for name in ADVISORIES:
        entry = adv[name]
        if not isinstance(entry, dict) or set(entry) != {"status", "consecutive_failures"}:
            raise CorruptStateError(f"{name}: claves inválidas")
        if entry["status"] not in STATUSES:
            raise CorruptStateError(f"{name}: status {entry['status']!r} fuera de {STATUSES}")
        n = entry["consecutive_failures"]
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise CorruptStateError(f"{name}: consecutive_failures {n!r} no es un entero >= 0")
    return state


def read_state(path: Path) -> dict[str, Any] | None:
    """Estado válido, ``None`` si el archivo no existe; ``CorruptStateError`` si es inválido."""
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorruptStateError(f"ilegible como JSON: {exc}") from exc
    return _validate(raw)


def next_state(
    previous: Mapping[str, Any] | None, results: Mapping[str, str], run_id: str, now: str | None = None
) -> dict[str, Any]:
    """Estado siguiente: ``failed`` incrementa el contador, ``ok`` lo reinicia a cero.

    Ausencia de estado previo inicializa desde cero. Cada advisory es INDEPENDIENTE.
    """
    if set(results) != set(ADVISORIES):
        raise ValueError(f"results {sorted(results)} != {sorted(ADVISORIES)}")
    prev_adv = (previous or {}).get("advisories", {})
    advisories: dict[str, Any] = {}
    for name in ADVISORIES:
        status = results[name]
        if status not in STATUSES:
            raise ValueError(f"{name}: status {status!r} fuera de {STATUSES}")
        before = prev_adv.get(name, {}).get("consecutive_failures", 0) if isinstance(prev_adv, dict) else 0
        streak = (before + 1) if status == FAILED else 0
        advisories[name] = {"status": status, "consecutive_failures": streak}
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": now or _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        "run_id": str(run_id),
        "advisories": advisories,
    }


def write_state_atomic(path: Path, state: Mapping[str, Any]) -> None:
    """Escribe el estado de forma atómica (tmp en el mismo directorio + ``os.replace``)."""
    _validate(dict(state))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".advisory_state.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def failed_count(results: Mapping[str, str]) -> int:
    """N de fallos del run, DERIVADO de los marcadores; siempre entre 0 y len(ADVISORIES)."""
    return sum(1 for name in ADVISORIES if results.get(name) != OK)


def n_pairs_live(path: Path) -> int | None:
    """``n_pairs_live`` del gate prospectivo; ``None`` si falta, es ilegible o no es entero.

    Nunca se tipea a mano: sale de ``reports/governance/promotion_decision.json``. Un valor
    ausente, corrupto o de tipo incorrecto se reporta honestamente como ``n/d``.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("n_pairs_live")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def issue_action(state: Mapping[str, Any] | None, previous: Mapping[str, Any] | None) -> str:
    """``open`` | ``close`` | ``none``: qué hacer con el issue único de advisories.

    - ``open``: algún advisory alcanza ``ISSUE_THRESHOLD`` fallos consecutivos (el primer
      fallo NO abre). Corridas posteriores mientras siga fallando devuelven ``open``: el
      paso del workflow es idempotente por label y solo crea si no hay uno abierto.
    - ``close``: todos vuelven a ``ok`` y en la corrida previa había un fallo acumulado.
    - ``none``: el resto.
    """
    if state is None:
        return "none"
    adv = state.get("advisories", {})
    if any(entry.get("consecutive_failures", 0) >= ISSUE_THRESHOLD for entry in adv.values()):
        return "open"
    if all(entry.get("status") == OK for entry in adv.values()):
        prev_adv = (previous or {}).get("advisories", {}) if previous else {}
        if any(entry.get("consecutive_failures", 0) > 0 for entry in prev_adv.values()):
            return "close"
    return "none"


def status_line(results: Mapping[str, str], state: Mapping[str, Any] | None, corrupt: str | None = None) -> str:
    """Línea del correo con AMBOS estados y sus rachas (o el aviso de estado corrupto)."""
    parts = []
    for name in ADVISORIES:
        streak = (state or {}).get("advisories", {}).get(name, {}).get("consecutive_failures", 0)
        suffix = f" (fallos consecutivos: {streak})" if streak else ""
        parts.append(f"{name}={results.get(name, FAILED)}{suffix}")
    line = " | ".join(parts)
    if corrupt:
        line += f" | ESTADO CORRUPTO PRESERVADO: {corrupt}"
    return line


def _parse_markers(pairs: list[str]) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for raw in pairs:
        name, _, value = raw.partition("=")
        if name not in ADVISORIES or not value:
            raise SystemExit(f"marcador inválido {raw!r}; se esperaba <{'|'.join(ADVISORIES)}>=<ruta>")
        out[name] = Path(value)
    if set(out) != set(ADVISORIES):
        raise SystemExit(f"faltan marcadores: {sorted(set(ADVISORIES) - set(out))}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Estado visible de los advisories del cron (D2-B).")
    ap.add_argument("command", choices=["update"])
    ap.add_argument("--state", required=True, type=Path)
    ap.add_argument("--marker", action="append", default=[], metavar="NOMBRE=RUTA")
    ap.add_argument("--promotion", type=Path, default=Path("reports/governance/promotion_decision.json"))
    ap.add_argument("--run-id", default="local")
    ap.add_argument("--out", type=Path, help="resumen JSON para el correo/issue")
    args = ap.parse_args(argv)

    markers = _parse_markers(args.marker)
    results = {name: read_marker(path) for name, path in markers.items()}

    corrupt: str | None = None
    previous: dict[str, Any] | None = None
    try:
        previous = read_state(args.state)
    except CorruptStateError as exc:
        corrupt = str(exc)
        print(f"::warning::estado de advisories corrupto, se PRESERVA sin sobrescribir: {exc}", file=sys.stderr)

    if corrupt is None:
        state = next_state(previous, results, args.run_id)
        write_state_atomic(args.state, state)
    else:
        # Estado corrupto: no se toca el archivo. El run sigue visible en el correo.
        state = next_state(None, results, args.run_id)

    n = failed_count(results)
    pairs = n_pairs_live(args.promotion)
    summary = {
        "failed_count": n,
        "subject_suffix": f"[{n} advisories fallidos]",
        "status_line": status_line(results, None if corrupt else state, corrupt),
        "n_pairs_live": "n/d" if pairs is None else str(pairs),
        "issue_action": "none" if corrupt else issue_action(state, previous),
        "state_written": corrupt is None,
        "corrupt": corrupt,
    }
    if args.out:
        args.out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
