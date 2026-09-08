"""E0 · La escala naïve del MASE tiene UNA sola implementación: ``vp_model.scale``.

Antes de E0 había cuatro copias de la misma fórmula: la canónica en ``vp_model.metrics``
(NaN ante escala degenerada, tras B4), una réplica declarada en ``run_statsforecast`` y
dos privadas (``improve_tabpfn``/``improve_timesfm``) que devolvían **1.0** — el fallback
silencioso que B4 había eliminado del canon, y que convierte el "MASE" en MAE en días.
Estas pruebas fijan la semántica única y corren en el job BASE (sin darts), que es donde
la canónica no se podía verificar: ``vp_model.metrics`` importa darts a nivel de módulo.
"""

from __future__ import annotations

import ast
import importlib
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
EXPERIMENTOS = ROOT / "experiments"
REPLICAS = {"seasonal_naive_mae", "naive_scale_before", "_naive_scale"}


# --------------------------------------------------------------------------- semántica
def test_valores_exactos_estacional_y_rw() -> None:
    """Los mismos valores que ancla D2, ahora verificables sin el extra ``model``."""
    from vp_model.scale import seasonal_naive_mae

    vals = np.arange(24, dtype=float)
    assert seasonal_naive_mae(vals, m=12) == 12.0
    assert seasonal_naive_mae(vals, m=1) == 1.0

    step = np.array([0.0] * 12 + [10.0] * 12)
    assert seasonal_naive_mae(step, m=12) == 10.0
    assert seasonal_naive_mae(step, m=1) == pytest.approx(10.0 / 23)


def test_historia_insuficiente_cae_a_primeras_diferencias() -> None:
    """``len(v) <= m`` ⇒ diferencias de orden 1; con menos de dos puntos no hay escala."""
    from vp_model.scale import seasonal_naive_mae

    corta = np.array([0.0, 3.0, 6.0])
    assert seasonal_naive_mae(corta, m=12) == 3.0  # |3-0|, |6-3|
    assert math.isnan(seasonal_naive_mae(np.array([5.0]), m=12))
    assert math.isnan(seasonal_naive_mae(np.array([], dtype=float), m=12))


def test_escala_cero_y_nan_son_indefinidas_nunca_uno() -> None:
    """Serie constante y serie con NaN ⇒ NaN. El 1.0 de las réplicas era una mentira."""
    from vp_model.scale import seasonal_naive_mae

    assert math.isnan(seasonal_naive_mae(np.full(30, 7.0), m=12))
    assert math.isnan(seasonal_naive_mae(np.array([1.0, np.nan, 3.0] * 10), m=12))
    assert math.isnan(seasonal_naive_mae(np.array([1.0, np.inf, 3.0] * 10), m=12))


@pytest.mark.parametrize("m", [0, -1, -12, 1.5, "12", None, True])
def test_periodo_estacional_invalido_falla_cerrado(m: object) -> None:
    """``m`` debe ser un entero ≥ 1. Antes: m=0 reventaba en numpy y m<0 daba basura."""
    from vp_model.scale import seasonal_naive_mae

    with pytest.raises(ValueError):
        seasonal_naive_mae(np.arange(24, dtype=float), m=m)  # type: ignore[arg-type]


def test_corte_por_fecha_no_por_posicion() -> None:
    """``naive_scale_before`` es leakage-free y robusta a huecos C/U (corte por FECHA)."""
    from vp_model.scale import naive_scale_before

    idx = pd.date_range("2010-01-01", periods=36, freq="MS")
    s = pd.Series(np.arange(36, dtype=float) * 12.0, index=idx)
    cutoff = idx[24]
    assert naive_scale_before(s, cutoff, m=12) == 12.0 * 12
    # el tramo posterior al corte no influye
    envenenada = s.copy()
    envenenada.iloc[24:] = 1e9
    assert naive_scale_before(envenenada, cutoff, m=12) == naive_scale_before(s, cutoff, m=12)


def test_escala_previa_degenerada_es_nan() -> None:
    from vp_model.scale import naive_scale_before

    idx = pd.date_range("2010-01-01", periods=36, freq="MS")
    plana = pd.Series(np.full(36, 4.0), index=idx)
    assert math.isnan(naive_scale_before(plana, idx[24], m=12))


# ------------------------------------------------------------------- entorno base
def test_importable_sin_darts() -> None:
    """El job base no instala darts: la fuente única debe importarse igual."""
    bloqueados = {"darts", "torch", "statsmodels", "lightgbm", "scipy", "optuna"}

    class _Bloqueo:
        def find_module(self, fullname, path=None):  # pragma: no cover - API vieja
            return None

        def find_spec(self, fullname, path=None, target=None):
            if fullname.split(".")[0] in bloqueados:
                raise ModuleNotFoundError(f"No module named {fullname!r}")
            return None

    previos = {n: m for n, m in sys.modules.items() if n.split(".")[0] in bloqueados}
    for nombre in previos:
        del sys.modules[nombre]
    # El módulo se re-importa DE CERO, así que hay que devolver el original a su sitio:
    # a ``sys.modules`` Y como ATRIBUTO del paquete ``vp_model`` (que es lo que resuelve
    # ``from vp_model import scale``). Si queda el duplicado, ``vp_model.metrics`` apunta
    # a las funciones del primero y la identidad se rompe — solo en la suite completa.
    paquete = importlib.import_module("vp_model")
    original = sys.modules.pop("vp_model.scale", None)
    sys.meta_path.insert(0, _Bloqueo())
    try:
        modulo = importlib.import_module("vp_model.scale")
        assert modulo.seasonal_naive_mae(np.arange(24, dtype=float), m=12) == 12.0
        assert modulo is not original, "no se re-importó: la prueba no probaría nada"
    finally:
        sys.meta_path.pop(0)
        sys.modules.update(previos)
        if original is not None:
            sys.modules["vp_model.scale"] = original
            paquete.__dict__["scale"] = original
    assert importlib.import_module("vp_model").__dict__["scale"] is original, "la prueba ensució el paquete"


def test_el_bloqueo_de_darts_es_fiel() -> None:
    """Si el bloqueo no bloquea, la prueba anterior no prueba nada (lección M48)."""
    bloqueados = {"darts"}

    class _Bloqueo:
        def find_spec(self, fullname, path=None, target=None):
            if fullname.split(".")[0] in bloqueados:
                raise ModuleNotFoundError(f"No module named {fullname!r}")
            return None

    previos = {n: m for n, m in sys.modules.items() if n.split(".")[0] == "darts"}
    for nombre in previos:
        del sys.modules[nombre]
    sys.meta_path.insert(0, _Bloqueo())
    try:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("darts")
    finally:
        sys.meta_path.pop(0)
        sys.modules.update(previos)


# --------------------------------------------------------------- anti-resurrección
def _defs_de(ruta: Path) -> set[str]:
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))
    return {
        n.name for n in ast.walk(arbol) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name in REPLICAS
    }


def test_ningun_experimento_redefine_la_escala() -> None:
    """Ningún módulo fuera de la fuente única vuelve a definir la fórmula."""
    culpables = {
        str(p.relative_to(ROOT)): sorted(_defs_de(p)) for p in sorted(EXPERIMENTOS.rglob("*.py")) if _defs_de(p)
    }
    assert culpables == {}, f"réplicas resucitadas: {culpables}"


def test_solo_vp_model_scale_define_la_formula() -> None:
    """Dentro del producto la definición vive en un único archivo."""
    definidores = {
        str(p.relative_to(ROOT))
        for p in sorted((ROOT / "vp_model").rglob("*.py")) + sorted((ROOT / "pipeline").rglob("*.py"))
        if _defs_de(p)
    }
    assert definidores == {"vp_model/scale.py"}, definidores


def test_los_experimentos_consumen_la_fuente_unica() -> None:
    """Los tres que replicaban ahora IMPORTAN de ``vp_model.scale``."""
    for nombre in ("run_statsforecast.py", "improve_tabpfn.py", "improve_timesfm.py"):
        arbol = ast.parse((EXPERIMENTOS / nombre).read_text(encoding="utf-8"))
        modulos = {n.module for n in ast.walk(arbol) if isinstance(n, ast.ImportFrom) and n.module}
        assert "vp_model.scale" in modulos, f"{nombre} no consume la fuente única"


def test_metrics_reexporta_la_misma_funcion() -> None:
    """Los importadores históricos de ``vp_model.metrics`` siguen válidos y NO son copias."""
    pytest.importorskip("darts")
    from vp_model import metrics, scale

    assert metrics.seasonal_naive_mae is scale.seasonal_naive_mae
    assert metrics.naive_scale_before is scale.naive_scale_before
