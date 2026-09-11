#!/usr/bin/env python3
"""Gate pre-push D1: bloquea el push si ``dvc.lock`` está desfasado del pipeline commiteado.

Es la ÚNICA implementación del gate: el paso «DVC lock matches the committed pipeline (E2)»
de ``.github/workflows/ci.yml`` lo invoca en vez de repetir la comprobación en bash, así que los
dos no pueden separarse (lo fija ``tests/test_dvc_lock_fresh.py``).

Vigila **seis** stages: los cinco *git-only* (``panel bulletins key_facts eda_facts fe_facts``)
y ``database`` (M72-R1, pendiente #57). ``scrape`` sigue fuera a propósito: sus dependencias son
``data/snapshots``, que es privado y no existe en un clon limpio.

⚠️ **Por qué ``database`` necesita una excepción acotada, medida y no supuesta.** Su out es un
artefacto CACHEADO. Con la caché DVC vacía —el caso de CI, que nunca hace ``dvc pull``—
``dvc status`` responde ``not in cache`` para ese out **igual de correcto que corrupto que
ausente**: ahí esa línea no lleva información. Con la caché presente —el caso del hook local—
responde ``{}``, ``modified`` o ``deleted``, y entonces sí la lleva. Medido en ambos contextos.
Por eso el gate tolera, **sólo en ``database``, sólo con ese texto exacto y sólo para el out
DECLARADO POR NOMBRE en ``CACHE_BACKED_OUTS``**, un residuo formado únicamente por outs
``not in cache``; vigila sus DEPS, que es donde el lock llevó semanas desfasado sin que nada lo
viera. En local el out queda cubierto de todas formas.

⚠️ La ruta importa tanto como el texto: M72-R1 comprobaba stage y valor pero no el nombre del out,
así que un out cacheado cualquiera pasaba por la excepción. Ahora el conjunto de rutas del residuo
debe ser **exactamente** el declarado —sin extras, sin ausentes y sin repetidos entre entradas— y
una prueba lo fija contra ``dvc.yaml`` y ``dvc.lock``.

El gate E2 detonó cuatro veces en julio por locks desfasados; este hook lo frena ANTES de
publicar (ver ``docs/DVC.md``).

Contrato (fail-closed):
- Usa el DVC gobernado del proyecto (``ante/bin/dvc`` relativo a la raíz, o la ruta en
  ``$VP_DVC``); si no existe o no es ejecutable, falla.
- Si ``dvc status`` termina con código distinto de cero, falla.
- Acepta ``{}`` o, como único residuo, el de caché descrito arriba; JSON inválido, claves JSON
  duplicadas, un tipo distinto de objeto, cualquier ``changed deps``, cualquier otro estado de
  out, una ruta no declarada y cualquier stage sin outs cacheados declarados fallan y se listan.
- No toca la red ni modifica nada; el remedio se imprime, nunca se aplica.

Uso: ``python tools/check_dvc_lock_fresh.py`` (sale 0 si el lock está al día, 1 si no).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
#: Stages vigilados. CI invoca este mismo módulo, así que la lista no puede divergir del workflow.
STAGES: tuple[str, ...] = ("panel", "bulletins", "key_facts", "eda_facts", "fe_facts", "database")
#: `scrape` queda fuera: depende de `data/snapshots`, privado y ausente en un clon limpio.
EXCLUDED_STAGES: frozenset[str] = frozenset({"scrape"})
#: Los outs cacheados tolerables, **declarados por nombre y stage**. No basta con que el stage sea
#: `database` y el texto sea el esperado: el conjunto de rutas del residuo debe ser EXACTAMENTE
#: éste. `tests/test_dvc_lock_fresh.py` lo fija contra `dvc.yaml` y `dvc.lock`, así que añadir un
#: out cacheado al DAG sin decidirlo aquí pone el gate en rojo en vez de colarse por la excepción.
CACHE_BACKED_OUTS: Mapping[str, frozenset[str]] = {
    "database": frozenset({"data/processed/visa_panel_long.parquet"}),
}
#: Único stage cuyo out vive en la caché DVC y no en git (derivado, no escrito dos veces).
CACHE_BACKED_STAGES: frozenset[str] = frozenset(CACHE_BACKED_OUTS)
#: Texto EXACTO de DVC cuando el objeto del out no está en la caché de este clon.
NOT_IN_CACHE = "not in cache"
DVC_ENV = "VP_DVC"
DEFAULT_DVC = Path("ante/bin/dvc")
REMEDY = "corre `make repro` y commitea dvc.lock junto con las salidas git-only que cambien"

Runner = Callable[[Sequence[str], Path], "subprocess.CompletedProcess[str]"]


def resolve_dvc(root: Path, env: Mapping[str, str]) -> Path | None:
    """DVC gobernado: ``$VP_DVC`` si está definido, si no ``<root>/ante/bin/dvc``. ``None`` si falta."""
    raw = env.get(DVC_ENV)
    candidate = Path(raw) if raw else DEFAULT_DVC
    if not candidate.is_absolute():
        candidate = root / candidate
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return candidate
    return None


def default_runner(cmd: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Ejecuta DVC sin red: ``status`` local, analítica desactivada, sin ``check`` (el código se evalúa)."""
    env = dict(os.environ)
    env["DVC_NO_ANALYTICS"] = "1"
    return subprocess.run(list(cmd), cwd=cwd, capture_output=True, text=True, env=env, check=False)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Una clave repetida en el JSON no puede colapsarse en silencio: `json` se queda la última.

    Sin esto, un `{"ruta": "not in cache", "ruta": "modified"}` llegaría al gate como una sola
    entrada tolerable. Mismo criterio que `tools/check_warnings.py` con el registro.
    """
    claves = [k for k, _ in pairs]
    repetidas = sorted({k for k in claves if claves.count(k) > 1})
    if repetidas:
        raise ValueError(f"la salida de `dvc status --json` trae claves duplicadas: {repetidas}")
    return dict(pairs)


def _is_cache_only_residue(stage: str, entries: object) -> bool:
    """¿El residuo de `stage` es SÓLO «el out DECLARADO no está en la caché de este clon»?

    Se exigen tres cosas, y las tres: el stage tiene outs cacheados declarados, **cada valor** es
    el texto exacto de DVC, y **el conjunto de rutas es EXACTAMENTE el declarado** — sin rutas de
    más, sin rutas de menos y sin repetir una ruta entre entradas.

    ⚠️ La primera versión (M72-R1) comprobaba stage y texto pero **no la ruta**, así que un out
    cacheado cualquiera —uno añadido al DAG, o el esperado acompañado de otro— pasaba por la
    excepción. Lo encontró la auditoría del autor; el RED vive en `tests/test_dvc_lock_fresh.py`.

    Fail-closed por construcción: cualquier `changed deps`, cualquier otro valor de out
    (`modified`, `deleted`, …), una forma inesperada o un stage sin outs cacheados declarados
    devuelven ``False`` y el gate falla. La tolerancia no puede crecer por accidente.
    """
    esperados = CACHE_BACKED_OUTS.get(stage)
    if esperados is None or not isinstance(entries, list) or not entries:
        return False
    vistas: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"changed outs"}:
            return False
        outs = entry["changed outs"]
        if not isinstance(outs, dict) or not outs:
            return False
        for path, value in outs.items():
            if not isinstance(path, str) or value != NOT_IN_CACHE:
                return False
            vistas.append(path)
    # igualdad EXACTA del conjunto y sin duplicados entre entradas
    return len(vistas) == len(esperados) and set(vistas) == esperados


def _blocking(status: Mapping[str, object]) -> dict[str, object]:
    """Lo que de verdad bloquea: todo menos el residuo de caché tolerado."""
    return {stage: detail for stage, detail in status.items() if not _is_cache_only_residue(stage, detail)}


def _describe(status: Mapping[str, object]) -> str:
    lines = []
    for stage in sorted(status):
        detail = json.dumps(status[stage], ensure_ascii=False)
        lines.append(f"  - {stage}: {detail[:200]}")
    return "\n".join(lines)


def evaluate(root: Path, runner: Runner, env: Mapping[str, str]) -> tuple[bool, str]:
    """Devuelve ``(ok, mensaje)``; ``ok`` solo con ``dvc status --json`` == ``{}``."""
    dvc = resolve_dvc(root, env)
    if dvc is None:
        return False, (
            f"✗ dvc-lock-fresh: no se encontró el DVC gobernado ({DEFAULT_DVC} bajo {root} o ${DVC_ENV}); "
            "fail-closed: el push se bloquea hasta tener el venv del proyecto"
        )
    cmd = [str(dvc), "status", "--json", *STAGES]
    result = runner(cmd, root)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        return False, f"✗ dvc-lock-fresh: `dvc status` terminó con código {result.returncode}\n  {err[:400]}"
    try:
        status = json.loads(result.stdout, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        return False, f"✗ dvc-lock-fresh: la salida de `dvc status --json` no es JSON válido ({exc})"
    except ValueError as exc:
        return False, f"✗ dvc-lock-fresh: {exc}"
    if not isinstance(status, dict):
        return False, f"✗ dvc-lock-fresh: la salida de `dvc status --json` no es un objeto ({type(status).__name__})"
    blocking = _blocking(status)
    if blocking:
        return False, (
            f"✗ dvc.lock desfasado del pipeline commiteado — stages: {', '.join(sorted(blocking))}\n"
            f"{_describe(blocking)}\n  Remedio: {REMEDY}"
        )
    if status:
        tolerado = ", ".join(sorted(status))
        return True, (
            f"✓ dvc.lock al día ({' '.join(STAGES)})"
            f" — sin caché local para el out de {tolerado}: sus DEPS sí quedan verificadas"
        )
    return True, f"✓ dvc.lock al día ({' '.join(STAGES)})"


def main(argv: Sequence[str] | None = None) -> int:
    ok, message = evaluate(ROOT, default_runner, os.environ)
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
