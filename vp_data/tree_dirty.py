"""Una sola definición comprobable de «el árbol está sucio» (C7b).

Vive en `vp_data` y no en `tools/` porque ADR-0001 fija la dirección de las capas: ni
`pipeline` ni `vp_model` pueden importar de `tools`, y sus dos consumidores están
precisamente ahí. `vp_data` es la capa que ambos ya importan.

Había tres, escritas por separado y con criterios distintos: dos miraban el árbol entero y
una tercera excluía `reports/` y `data/` a propósito, porque las etapas de campaña
reescriben sus propias salidas y el guardián no debe abortar por ellas. La diferencia era
real y estaba bien razonada; lo que faltaba era decirla en un solo sitio en vez de
deducirla leyendo tres implementaciones.

Aquí el alcance es un argumento explícito:

* `Scope.ALL` — cualquier cambio sin commitear, incluidos artefactos regenerados.
* `Scope.CODE` — solo rutas de código; `reports/` y `data/` quedan fuera.

Cuando git no está disponible se devuelve `None`, nunca `False`: «no lo sé» y «está limpio»
no son lo mismo, y grabar lo segundo por lo primero fabrica una identidad de build.

Stdlib puro: lo consumen tanto la capa de datos como la de modelado.
"""

from __future__ import annotations

import subprocess
from enum import Enum
from pathlib import Path

__all__ = ["Scope", "REPO_ROOT", "porcelain", "tree_dirty"]

REPO_ROOT = Path(__file__).resolve().parents[1]


class Scope(Enum):
    """Qué se mira para decidir si el árbol está sucio."""

    ALL = "all"
    CODE = "code"

    @property
    def pathspec(self) -> tuple[str, ...]:
        """Los argumentos de ruta que `git status` recibe para este alcance."""
        if self is Scope.CODE:
            # Las etapas de campaña reescriben salidas rastreadas bajo reports/ y data/;
            # un guardián de código no debe abortar por los artefactos de su propia corrida.
            return ("--", ".", ":(exclude)reports", ":(exclude)data")
        return ()


def porcelain(scope: Scope = Scope.ALL, root: Path | None = None) -> str | None:
    """La salida cruda de `git status --porcelain` para este alcance, o `None` sin git."""
    try:
        run = subprocess.run(
            ["git", "status", "--porcelain", *scope.pathspec],
            capture_output=True,
            text=True,
            cwd=root or REPO_ROOT,
            check=False,
        )
    except OSError:
        return None
    return run.stdout if run.returncode == 0 else None


def tree_dirty(scope: Scope = Scope.ALL, root: Path | None = None) -> bool | None:
    """¿Hay cambios sin commitear en este alcance?

    `None` significa que no se pudo averiguar (sin git, o `git status` falló). Quien
    escriba una identidad de build debe propagar ese `None` como NULL, no convertirlo en
    `False`.
    """
    salida = porcelain(scope, root)
    return None if salida is None else bool(salida.strip())
