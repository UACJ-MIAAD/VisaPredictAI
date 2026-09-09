"""Ninguna prueba del job base puede importar el extra de modelado. Para TODA la suite.

Quinta reincidencia (M14, M48, M59, M62, M64). La cuarta la cerré con una meta-prueba… **dentro
de un solo archivo**, así que protegía ese archivo y ninguno más: la quinta entró por un archivo
nuevo. Un guardián con el alcance de un archivo cubre un archivo.

Aquí el alcance es la suite entera: se recorre el AST de **cada** ``tests/*.py`` y se marca todo
import del extra que no esté protegido. Un archivo entero puede estar protegido por un
``pytest.importorskip`` de módulo o por la lista ``_MODEL_TESTS`` del conftest; una función, por
un ``skipif`` que mencione el módulo pesado.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

#: Lo que el job base NO instala (`.[dev]` sin el extra `model`).
PESADOS = {"scipy", "darts", "statsmodels", "lightgbm", "xgboost", "torch", "optuna", "neuralforecast"}


#: Qué módulos del producto son pesados NO se mantiene a mano: se DERIVA leyendo sus imports de
#: nivel de módulo y siguiendo los de ``vp_model`` en cascada. Una lista escrita a mano se queda
#: rancia igual que cualquier otro literal.
def _pesado_por_cascada(modulo: str, visto: frozenset[str] = frozenset()) -> bool:
    ruta = ROOT / Path(modulo.replace(".", "/") + ".py")
    if modulo in visto or not ruta.exists():
        return False
    cabecera = ast.parse(ruta.read_text(encoding="utf-8").split("\ndef ", 1)[0])
    for nodo in ast.walk(cabecera):
        nombres: list[str] = []
        if isinstance(nodo, ast.ImportFrom) and nodo.module:
            nombres = [f"{nodo.module}.{a.name}" for a in nodo.names] if nodo.module == "vp_model" else [nodo.module]
        elif isinstance(nodo, ast.Import):
            nombres = [a.name for a in nodo.names]
        for nombre in nombres:
            raiz = nombre.split(".")[0]
            if raiz in PESADOS:
                return True
            if raiz == "vp_model" and _pesado_por_cascada(nombre, visto | {modulo}):
                return True
    return False


def _model_tests() -> set[str]:
    """Archivos que el conftest salta enteros cuando falta el extra."""
    arbol = ast.parse((TESTS / "conftest.py").read_text(encoding="utf-8"))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_MODEL_TESTS" for t in nodo.targets
        ):
            elementos = getattr(nodo.value, "elts", [])
            return {str(e.value) for e in elementos if isinstance(e, ast.Constant)}
    return set()


def _archivo_protegido(texto: str, arbol: ast.Module) -> bool:
    """`pytest.importorskip("pesado")` a nivel de módulo protege todo el archivo."""
    for nodo in arbol.body:
        if not isinstance(nodo, ast.Expr) or not isinstance(nodo.value, ast.Call):
            continue
        f = nodo.value.func
        if isinstance(f, ast.Attribute) and f.attr == "importorskip":
            for arg in nodo.value.args:
                if isinstance(arg, ast.Constant) and str(arg.value).split(".")[0] in PESADOS:
                    return True
    return False


def _decorador_protege(nodo: ast.FunctionDef | ast.ClassDef, texto: str) -> bool:
    """Un `skipif` (directo o por constante local) que mencione un módulo pesado."""
    for d in nodo.decorator_list:
        fuente = ast.get_source_segment(texto, d) or ""
        if "skipif" in fuente and any(p in fuente for p in PESADOS):
            return True
        # marcadores locales del estilo `PROFUNDO = pytest.mark.skipif(find_spec("darts")...)`
        nombre = fuente.strip().lstrip("@").split("(")[0]
        if nombre.isupper() and nombre in texto:
            for linea in texto.splitlines():
                if linea.startswith(f"{nombre} =") and any(p in linea for p in PESADOS):
                    return True
            bloque = texto.split(f"{nombre} = ", 1)
            if len(bloque) > 1 and any(p in bloque[1][:400] for p in PESADOS):
                return True
    return False


def _cuerpo_protege(nodo: ast.AST) -> bool:
    """Un ``importorskip`` dentro de la función la protege, sea cual sea el módulo.

    Vale CUALQUIERA y no solo los de ``PESADOS``: `test_chronos_loader_hardened` salta con
    ``importorskip("chronos")`` —un extra que no está en esa lista— y solo después importa
    ``vp_model.models``. Exigir que el nombre esté en la lista convertía un guardián correcto en
    un falso positivo, y mantener la lista al día es justo lo que este archivo evita.
    """
    for sub in ast.walk(nodo):
        f = getattr(sub, "func", None)
        if isinstance(sub, ast.Call) and isinstance(f, ast.Attribute) and f.attr == "importorskip":
            return True
    return False


def _imports_pesados(nodo: ast.AST) -> list[str]:
    fuera: list[str] = []
    for sub in ast.walk(nodo):
        if isinstance(sub, ast.ImportFrom) and sub.module:
            raiz = sub.module.split(".")[0]
            if raiz in PESADOS:
                fuera.append(sub.module)
            elif raiz == "vp_model":
                nombres = [f"vp_model.{a.name}" for a in sub.names] if sub.module == "vp_model" else [sub.module]
                fuera += [m for m in nombres if _pesado_por_cascada(m)]
        elif isinstance(sub, ast.Import):
            for a in sub.names:
                raiz = a.name.split(".")[0]
                if raiz in PESADOS or (raiz == "vp_model" and _pesado_por_cascada(a.name)):
                    fuera.append(a.name)
    return fuera


def test_ninguna_prueba_del_job_base_importa_el_extra_de_modelado() -> None:
    gateados = _model_tests()
    ofensas: list[str] = []
    for ruta in sorted(TESTS.glob("test_*.py")):
        if ruta.name in gateados or ruta.name == Path(__file__).name:
            continue
        texto = ruta.read_text(encoding="utf-8")
        arbol = ast.parse(texto)
        if _archivo_protegido(texto, arbol):
            continue
        for cabecera in arbol.body:
            if isinstance(cabecera, ast.Import | ast.ImportFrom):
                for m in _imports_pesados(cabecera):
                    ofensas.append(f"{ruta.name}: import de módulo `{m}` sin protección")
        for nodo in arbol.body:
            if not isinstance(nodo, ast.FunctionDef | ast.ClassDef):
                continue
            if _decorador_protege(nodo, texto):
                continue
            hijos = (
                [n for n in nodo.body if isinstance(n, ast.FunctionDef)] if isinstance(nodo, ast.ClassDef) else [nodo]
            )
            for hijo in hijos:
                if isinstance(nodo, ast.ClassDef) and _decorador_protege(hijo, texto):
                    continue
                if _cuerpo_protege(hijo):
                    continue
                for m in _imports_pesados(hijo):
                    ofensas.append(f"{ruta.name}::{hijo.name} importa `{m}`")
    assert ofensas == [], "pruebas del job base con el extra de modelado:\n  " + "\n  ".join(ofensas)


def test_la_cascada_distingue_ligeros_de_pesados() -> None:
    """La derivación no es decorativa: separa lo que de verdad arrastra el extra."""
    assert not _pesado_por_cascada("vp_model.universe")
    assert not _pesado_por_cascada("vp_model.scale")
    assert not _pesado_por_cascada("vp_model.deck")
    assert _pesado_por_cascada("vp_model.metrics"), "metrics importa darts"
    assert _pesado_por_cascada("vp_model.horizon"), "horizon importa scipy y arrastra models"
