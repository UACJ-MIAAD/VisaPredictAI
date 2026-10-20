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

#: Lo que el job base NO instala (`.[dev]` sin el extra `model`): una sola lista, la del conftest.
from tests.conftest import PESADOS  # noqa: E402


#: Qué módulos del producto son pesados NO se mantiene a mano: se DERIVA leyendo sus imports de
#: nivel de módulo y siguiendo los de ``vp_model`` en cascada. Una lista escrita a mano se queda
#: rancia igual que cualquier otro literal.
def _imports_de_nivel_de_modulo(arbol: ast.AST) -> list[ast.stmt]:
    """Los `import` que se ejecutan AL IMPORTAR: los de nivel de módulo, `if`/`try` incluidos.

    ⚠️ Antes esto se hacía recortando la fuente en el primer ``"\ndef "`` y analizando el trozo.
    El recorte deja colgando el decorador de la primera función cuando la hay
    (``@contextmanager`` + ``def``), y `ast.parse` revienta con `SyntaxError`. `vp_model/noise.py`
    fue el primer módulo del repositorio cuya única función está decorada, y destapó el defecto.
    Se recorre el árbol completo saltando los cuerpos de funciones y clases, que es lo que el
    recorte quería aproximar: un import dentro de una función NO se ejecuta al importar.
    """
    fuera: list[ast.stmt] = []
    pila: list[ast.AST] = [arbol]
    while pila:
        nodo = pila.pop()
        for hijo in ast.iter_child_nodes(nodo):
            if isinstance(hijo, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                continue
            if isinstance(hijo, ast.Import | ast.ImportFrom):
                fuera.append(hijo)
            pila.append(hijo)
    return fuera


#: G7 · desbloqueo: paquetes PROPIOS cuya cascada se sigue. Antes sólo `vp_model`, y por ahí se colaron cinco ofensas
#: que el runner Linux destapó: `score_e3_campaign` (experiments) → `vp_model.metrics` → darts, `tools.campaign_preflight`
#: → `vp_model.champion` → scipy, y un `run_global_gbm` (experiments) → xgboost.
PROPIOS = ("vp_model", "tools", "pipeline", "vp_data")
#: Carpetas que las pruebas ponen en `sys.path`: ahí un módulo se importa por su nombre suelto (`import check_contracts`).
EN_SYS_PATH = ("experiments", "tools")


def _ruta_de(modulo: str) -> Path | None:
    """El archivo propio que importa `modulo`, o ``None`` si no es nuestro."""
    raiz = modulo.split(".")[0]
    if raiz in PROPIOS:
        ruta = ROOT / Path(modulo.replace(".", "/") + ".py")
        return ruta if ruta.exists() else None
    if "." not in modulo:
        for carpeta in EN_SYS_PATH:
            ruta = ROOT / carpeta / f"{modulo}.py"
            if ruta.exists():
                return ruta
    return None


def _pesado_por_cascada(modulo: str, visto: frozenset[str] = frozenset()) -> bool:
    ruta = _ruta_de(modulo)
    if modulo in visto or ruta is None:
        return False
    for nodo in _imports_de_nivel_de_modulo(ast.parse(ruta.read_text(encoding="utf-8"))):
        nombres: list[str] = []
        if isinstance(nodo, ast.ImportFrom) and nodo.module:
            nombres = [f"{nodo.module}.{a.name}" for a in nodo.names] if nodo.module in PROPIOS else [nodo.module]
        elif isinstance(nodo, ast.Import):
            nombres = [a.name for a in nodo.names]
        for nombre in nombres:
            if nombre.split(".")[0] in PESADOS:
                return True
            if _pesado_por_cascada(nombre, visto | {modulo}):
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


def _linea_protectora(arbol: ast.Module) -> int | None:
    """Línea del primer `pytest.importorskip("pesado")` de nivel de módulo (expresión o asignación), o ``None``.

    ⚠️ G7 · desbloqueo: protege sólo lo que viene DESPUÉS. `test_exclusiones_acreditadas_m74e_r20` importaba
    `vp_model.persist_forecasts` (→ xgboost) en la línea 27 y saltaba con `importorskip("darts")` en la 29; el
    guardián daba el archivo por protegido y el runner Linux murió al recolectarlo.
    """
    for nodo in arbol.body:
        valor = nodo.value if isinstance(nodo, ast.Expr | ast.Assign) else None
        if not isinstance(valor, ast.Call):
            continue
        f = valor.func
        if isinstance(f, ast.Attribute) and f.attr == "importorskip":
            for arg in valor.args:
                if isinstance(arg, ast.Constant) and str(arg.value).split(".")[0] in PESADOS:
                    return nodo.lineno
    return None


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


def _es_prueba_o_fixture(nodo: ast.FunctionDef | ast.ClassDef) -> bool:
    """Lo que pytest ejecuta por sí mismo: pruebas, clases de prueba y fixtures. Un ayudante se juzga por quien lo llama
    (su import pesado sólo corre si una prueba lo invoca, y esa prueba sí se revisa o la delata el runner)."""
    if isinstance(nodo, ast.ClassDef) or nodo.name.startswith("test_"):
        return True
    return any("fixture" in ast.unparse(d) for d in nodo.decorator_list)


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
        # G7 · desbloqueo: `requiere(modulos=…)` del conftest omite en dev y FALLA en el perfil `model`
        if isinstance(sub, ast.Call) and isinstance(f, ast.Name) and f.id == "requiere":
            return True
    return False


def _imports_pesados(nodo: ast.AST) -> list[str]:
    fuera: list[str] = []
    for sub in ast.walk(nodo):
        if isinstance(sub, ast.ImportFrom) and sub.module:
            raiz = sub.module.split(".")[0]
            if raiz in PESADOS:
                fuera.append(sub.module)
            else:
                nombres = [f"{sub.module}.{a.name}" for a in sub.names] if sub.module in PROPIOS else [sub.module]
                fuera += [m for m in nombres if _pesado_por_cascada(m)]
        elif isinstance(sub, ast.Import):
            for a in sub.names:
                if a.name.split(".")[0] in PESADOS or _pesado_por_cascada(a.name):
                    fuera.append(a.name)
    return fuera


def ofensas_en(directorio: Path, gateados: set[str]) -> list[str]:
    """Las pruebas de `directorio` que importan el extra sin protección (archivos de `gateados` excluidos)."""
    ofensas: list[str] = []
    for ruta in sorted(directorio.glob("test_*.py")):
        if ruta.name in gateados or ruta.name == Path(__file__).name:
            continue
        texto = ruta.read_text(encoding="utf-8")
        arbol = ast.parse(texto)
        protectora = _linea_protectora(arbol)
        for cabecera in arbol.body:
            if isinstance(cabecera, ast.Import | ast.ImportFrom) and (
                protectora is None or cabecera.lineno < protectora
            ):
                for m in _imports_pesados(cabecera):
                    ofensas.append(f"{ruta.name}: import de módulo `{m}` sin protección")
        if protectora is not None:
            continue
        for nodo in arbol.body:
            if not isinstance(nodo, ast.FunctionDef | ast.ClassDef) or not _es_prueba_o_fixture(nodo):
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
    return ofensas


def test_ninguna_prueba_del_job_base_importa_el_extra_de_modelado() -> None:
    ofensas = ofensas_en(TESTS, _model_tests())
    assert ofensas == [], "pruebas del job base con el extra de modelado:\n  " + "\n  ".join(ofensas)


def test_la_cascada_distingue_ligeros_de_pesados() -> None:
    """La derivación no es decorativa: separa lo que de verdad arrastra el extra."""
    assert not _pesado_por_cascada("vp_model.universe")
    assert not _pesado_por_cascada("vp_model.scale")
    assert not _pesado_por_cascada("vp_model.deck")
    assert not _pesado_por_cascada("vp_model.noise")  # M72: stdlib puro, y por eso importable en el job base
    assert _pesado_por_cascada("vp_model.metrics"), "metrics importa darts"
    assert _pesado_por_cascada("vp_model.horizon"), "horizon importa scipy y arrastra models"


def test_la_cascada_soporta_un_modulo_cuya_primera_funcion_esta_decorada(tmp_path: Path) -> None:
    """RED del recorte por `"\ndef "`: dejaba el decorador colgando y `ast.parse` reventaba.

    Se comprueba además que el veredicto es el correcto en los dos sentidos, no sólo que no
    explota: un módulo decorado ligero da `False` y uno decorado que importa el extra da `True`.
    """
    import ast as _ast

    ligero = _ast.parse("from contextlib import contextmanager\n\n\n@contextmanager\ndef f():\n    yield\n")
    assert _imports_de_nivel_de_modulo(ligero) != []
    pesado = _ast.parse(
        "import statsmodels\nfrom contextlib import contextmanager\n\n\n@contextmanager\ndef f():\n    yield\n"
    )
    nombres = {a.name for n in _imports_de_nivel_de_modulo(pesado) if isinstance(n, _ast.Import) for a in n.names}
    assert "statsmodels" in nombres
    # …y un import DENTRO de una función no cuenta: no se ejecuta al importar el módulo
    local = _ast.parse("def f():\n    import statsmodels\n    return statsmodels\n")
    assert _imports_de_nivel_de_modulo(local) == []


# ═══════════════════════════════ G7 · desbloqueo: los huecos del reconocedor que el runner Linux destapó
def _sembrar(tmp_path: Path, nombre: str, codigo: str) -> Path:
    (tmp_path / nombre).write_text(codigo, encoding="utf-8")
    return tmp_path


def test_un_importorskip_posterior_al_import_pesado_no_protege(tmp_path: Path) -> None:
    """RED de r20: el import pesado corría ANTES del `importorskip`, y el archivo se daba por protegido."""
    d = _sembrar(
        tmp_path, "test_tarde.py", 'import pytest\nfrom vp_model import metrics\npytest.importorskip("darts")\n'
    )
    assert ofensas_en(d, set()) == ["test_tarde.py: import de módulo `vp_model.metrics` sin protección"]


def test_control_un_importorskip_anterior_protege_y_su_forma_asignada_tambien(tmp_path: Path) -> None:
    _sembrar(tmp_path, "test_antes.py", 'import pytest\npytest.importorskip("darts")\nfrom vp_model import metrics\n')
    d = _sembrar(
        tmp_path, "test_asig.py", 'import pytest\ndarts = pytest.importorskip("darts")\nfrom vp_model import metrics\n'
    )
    assert ofensas_en(d, set()) == []


def test_la_cascada_sigue_experiments_y_tools(tmp_path: Path) -> None:
    """RED de r17/m74f: `score_e3_campaign` (experiments) → `vp_model.metrics` → darts no se seguía."""
    d = _sembrar(tmp_path, "test_exp.py", "import score_e3_campaign\n")
    _sembrar(tmp_path, "test_tool.py", "def test_x():\n    import check_env_matches_lock\n")
    ofensas = ofensas_en(d, set())
    assert ofensas == ["test_exp.py: import de módulo `score_e3_campaign` sin protección"], ofensas


def test_control_un_ayudante_se_juzga_por_quien_lo_llama_y_requiere_protege(tmp_path: Path) -> None:
    codigo = (
        "from tests.conftest import requiere\n\n"
        "def _ayudante():\n    import score_e3_campaign\n\n"
        'def test_protegida():\n    requiere(modulos=("darts",))\n    import score_e3_campaign\n'
    )
    assert ofensas_en(_sembrar(tmp_path, "test_ayuda.py", codigo), set()) == []
