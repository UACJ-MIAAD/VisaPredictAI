"""Formato de rutas para la procedencia de artefactos generados. Dependency-light.

Un artefacto que declara de dónde salió necesita nombrar sus entradas de forma estable: la ruta
relativa al repositorio cuando vive dentro, y el nombre del archivo cuando no (un panel o un
catálogo de prueba en un directorio temporal). ``relative_to`` a secas **lanza** en ese segundo
caso, que es como se rompieron los generadores de E1 y E2 al correr sobre material sintético.

Vive en el producto y no en ``experiments/`` porque lo usan dos generadores distintos y un
``from experiments.<otro> import ...`` no resuelve cuando el script se ejecuta directamente:
``experiments`` no es importable sin meter la raíz en ``sys.path``.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["REPO_ROOT", "ruta_legible"]

#: Raíz del repositorio, derivada de la ubicación de este módulo (no de un cwd ni de una env).
REPO_ROOT = Path(__file__).resolve().parent.parent


def ruta_legible(ruta: Path, raiz: Path | None = None) -> str:
    """Relativa a ``raiz`` cuando la ruta vive dentro; si no, su nombre."""
    base = raiz or REPO_ROOT
    try:
        return str(Path(ruta).resolve().relative_to(base))
    except ValueError:
        return Path(ruta).name
