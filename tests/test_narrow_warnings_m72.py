"""M72: las nueve supresiones amplias de los productores, una por una.

Cada sitio tiene **dos** pruebas, y las dos miran COMPORTAMIENTO, no texto:

* **RED conductual** — se inyecta un aviso ajeno en la llamada que el sitio envolvía y se exige
  que **llegue a la superficie**. Contra el código anterior, con su
  ``simplefilter("ignore")``, ese aviso moría ahí: cada una de estas pruebas falla sobre
  ``main@a8029f2``. Eso es lo que las hace discriminantes y no decorativas.
* **Control benigno** — se comprueba que lo que el sitio tiene derecho a silenciar **sigue
  silenciado** (y que la función devuelve lo mismo). Sin este control, "retirar la supresión"
  podría degenerar en llenar de ruido el camino de ejecución, que es la razón por la que las
  supresiones se pusieron.

La inyección se hace sustituyendo la dependencia que el sitio invoca por una envoltura que avisa
y delega: así se ejercita el gestor de contexto REAL del productor, no una réplica.

Los tres corredores (``auto_arima_baseline``, ``freeze_shadow``, ``generate_web_forecasts``) se
prueban por su punto de entrada, inyectando el aviso en lo primero que toca el cuerpo envuelto y
cortando la corrida con un centinela: ajustar 25 series para comprobar un filtro sería pagar una
campaña por una asersión.
"""

from __future__ import annotations

import ast
import warnings
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import pytest

from vp_model.noise import known_fit_warnings, silenced_fit_warnings

RAIZ = Path(__file__).resolve().parents[1]
MODELADO = pytest.mark.skipif(find_spec("statsmodels") is None, reason="requiere el extra `model` (statsmodels)")

#: Aviso ajeno: ni está registrado, ni lo emite ninguna dependencia. Si aparece en el registro de
#: avisos es porque atravesó el productor.
AJENO = "AVISO AJENO QUE NINGUN PRODUCTOR TIENE DERECHO A TRAGARSE"


class _Ajeno(UserWarning):
    """Categoría propia: nadie puede alegar que la silenció por casualidad."""


def _avisa_y_delega(real, mensaje: str = AJENO, categoria: type[Warning] = _Ajeno):
    """Envoltura que emite `mensaje` y luego delega en `real`."""

    def envoltura(*args, **kwargs):
        warnings.warn(mensaje, categoria, stacklevel=2)
        return real(*args, **kwargs)

    return envoltura


def _avisa_y_revienta(mensaje: str, categoria: type[Warning], centinela: type[BaseException]):
    """Envoltura que emite y corta: para corredores cuyo cuerpo completo es una campaña."""

    def envoltura(*args, **kwargs):
        warnings.warn(mensaje, categoria, stacklevel=2)
        raise centinela("corte deliberado de la prueba")

    return envoltura


class _Corte(Exception):
    """Centinela para detener un corredor en su primera llamada."""


def _capturado(fn, *args, **kwargs) -> tuple[list[warnings.WarningMessage], Any]:
    """Ejecuta `fn` registrando TODOS los avisos, sin que el `error` global de la suite estorbe."""
    with warnings.catch_warnings(record=True) as registro:
        warnings.simplefilter("always")
        valor = fn(*args, **kwargs)
    return list(registro), valor


def _mensajes(registro) -> list[str]:
    return [str(w.message) for w in registro]


def _serie_sintetica(n: int = 60):
    import numpy as np
    import pandas as pd

    idx = pd.date_range("2015-01-01", periods=n, freq="MS")
    rng = np.random.default_rng(20260910)
    return pd.Series(np.arange(n, dtype="float64") * 30.0 + rng.normal(0, 5, n), index=idx)


# ===========================================================================================
# La envoltura compartida: vp_model/noise.py
# ===========================================================================================
def test_la_envoltura_silencia_los_cuatro_avisos_declarados() -> None:
    """Control benigno del helper: los cuatro mensajes registrados no salen."""
    for prefijo in known_fit_warnings():
        registro, _ = _capturado(lambda p=prefijo: _emite(p))
        assert _mensajes(registro) == [], f"{prefijo!r} debería estar silenciado"


def _emite(mensaje: str, categoria: type[Warning] = UserWarning) -> None:
    with silenced_fit_warnings():
        warnings.warn(mensaje, categoria, stacklevel=2)


def test_la_envoltura_deja_pasar_cualquier_otro_aviso() -> None:
    """RED del helper: lo no declarado atraviesa, aunque comparta categoría."""
    registro, _ = _capturado(lambda: _emite(AJENO))
    assert _mensajes(registro) == [AJENO]


@pytest.mark.parametrize(
    "vecino",
    [
        "Non-stationary starting parameters found",  # falta «autoregressive»
        "Maximum Likelihood optimization failed",  # se queda antes de «to converge»
        "The optimization failed to converge",  # el prefijo no ancla al inicio
    ],
)
def test_la_envoltura_no_silencia_mensajes_vecinos(vecino: str) -> None:
    """El filtro es por PREFIJO LITERAL: un mensaje parecido no queda cubierto."""
    registro, _ = _capturado(lambda: _emite(vecino))
    assert _mensajes(registro) == [vecino]


def test_la_envoltura_se_restaura_aunque_el_cuerpo_lance() -> None:
    """Un fallo a mitad no puede dejar filtros instalados: el estado vuelve exacto."""
    antes = list(warnings.filters)
    with pytest.raises(_Corte), silenced_fit_warnings():
        raise _Corte("boom")
    assert list(warnings.filters) == antes


def test_los_cuatro_mensajes_estan_registrados_como_excepciones_upstream() -> None:
    """Cada mensaje que la envoltura silencia tiene su entrada versionada y con revisión."""
    import json

    registro = json.loads((RAIZ / "security" / "warnings_registry.json").read_text(encoding="utf-8"))
    prefijos = [e["message_prefix"] for e in registro["warnings"]]
    for conocido in known_fit_warnings():
        assert any(p.startswith(conocido) for p in prefijos), f"{conocido!r} se silencia sin registrar"


# ===========================================================================================
# 1/9 · vp_model/eda.py:79 — stationarity_of
# ===========================================================================================
@MODELADO
class TestEdaStationarity:
    def test_red_un_aviso_de_kpss_ya_no_muere_ahi(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from vp_model import eda

        monkeypatch.setattr(eda, "kpss", _avisa_y_delega(eda.kpss))
        registro, valor = _capturado(eda.stationarity_of, _serie_sintetica())
        assert AJENO in _mensajes(registro)
        assert isinstance(valor["kpss_pvalue"], float)

    def test_red_un_aviso_de_adfuller_ya_no_muere_ahi(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ADF quedó FUERA del gestor: medido, no avisa en ninguna serie del panel."""
        from vp_model import eda

        monkeypatch.setattr(eda, "adfuller", _avisa_y_delega(eda.adfuller))
        registro, _ = _capturado(eda.stationarity_of, _serie_sintetica())
        assert AJENO in _mensajes(registro)

    def test_red_la_degradacion_ruidosa_de_df_gls_vuelve_a_oirse(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """El propio módulo llamaba «degradación RUIDOSA» a un aviso que la supresión se tragaba.

        Sin `arch`, DF-GLS se reporta NaN y se avisa. Bajo `simplefilter("ignore")` ese aviso
        no salía, así que la degradación era silenciosa justo donde se había escrito lo contrario.
        """
        import builtins

        from vp_model import eda

        real_import = builtins.__import__

        def sin_arch(name, *args, **kwargs):
            if name.startswith("arch"):
                raise ImportError("arch bloqueado por la prueba")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", sin_arch)
        registro, valor = _capturado(eda.stationarity_of, _serie_sintetica())
        assert any("DF-GLS no disponible" in m for m in _mensajes(registro))
        assert valor["dfgls_pvalue"] != valor["dfgls_pvalue"]  # NaN, la convención declarada

    def test_control_benigno_el_aviso_de_kpss_sigue_silenciado(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`InterpolationWarning` es lo único que esta llamada tiene derecho a tragarse."""
        from statsmodels.tools.sm_exceptions import InterpolationWarning

        from vp_model import eda

        monkeypatch.setattr(
            eda,
            "kpss",
            _avisa_y_delega(eda.kpss, "p-value is smaller than the indicated p-value", InterpolationWarning),
        )
        registro, valor = _capturado(eda.stationarity_of, _serie_sintetica())
        assert _mensajes(registro) == []
        assert set(valor) >= {"adf_pvalue", "kpss_pvalue", "dfgls_pvalue", "verdict"}


# ===========================================================================================
# 2/9 · vp_model/missingness.py:97 — kalman_impute (supresión retirada entera)
# ===========================================================================================
@MODELADO
class TestMissingnessKalman:
    @staticmethod
    def _con_huecos():
        serie = _serie_sintetica(48)
        serie.iloc[10:16] = float("nan")
        return serie

    def test_red_un_aviso_del_ajuste_ya_no_muere_ahi(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # `UnobservedComponents` se importa DENTRO de la función, así que el seam es su módulo.
        import statsmodels.tsa.statespace.structural as structural

        from vp_model import missingness

        monkeypatch.setattr(structural, "UnobservedComponents", _avisa_y_delega(structural.UnobservedComponents))
        registro, salida = _capturado(missingness.kalman_impute, self._con_huecos())
        assert AJENO in _mensajes(registro)
        assert salida.notna().all()

    def test_control_benigno_el_ajuste_real_no_emite_nada(self) -> None:
        """Medido sobre las 115 series con >=24 obs: cero avisos. Retirar la supresión no hace ruido.

        ⚠️ M74-E-R4 lo acotó a «sólo lo registrado» porque la regeneración en bloque había movido
        numpy a 2.5.3 y statsmodels emitía una deprecación. **M74-E-R5 preservó los pines pre-R4**,
        así que el control vuelve a su forma fuerte: NADA. Aflojar un control por un cambio de
        entorno que después se revierte lo dejaría flojo para siempre.
        """
        from vp_model import missingness

        serie = self._con_huecos()
        registro, salida = _capturado(lambda: missingness.kalman_impute(serie))
        assert _mensajes(registro) == []
        assert salida.notna().all() and len(salida) >= 48


# ===========================================================================================
# 3/9 y 4/9 · vp_model/series_characterization.py:120 (ndiffs) y :136 (ljung_box_pvalue)
# ===========================================================================================
@MODELADO
class TestCaracterizacionKpssYLjungBox:
    def test_red_un_aviso_de_kpss_en_ndiffs_ya_no_muere_ahi(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from vp_model import series_characterization as sc

        monkeypatch.setattr(sc, "kpss", _avisa_y_delega(sc.kpss))
        registro, valor = _capturado(sc.ndiffs, _serie_sintetica())
        assert AJENO in _mensajes(registro)
        assert isinstance(valor, int)

    def test_control_benigno_kpss_en_ndiffs_sigue_callando_su_interpolacion(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from statsmodels.tools.sm_exceptions import InterpolationWarning

        from vp_model import series_characterization as sc

        monkeypatch.setattr(
            sc, "kpss", _avisa_y_delega(sc.kpss, "p-value is greater than the indicated p-value", InterpolationWarning)
        )
        registro, valor = _capturado(sc.ndiffs, _serie_sintetica())
        assert _mensajes(registro) == [] and 0 <= valor <= 2

    def test_red_un_aviso_de_ljung_box_ya_no_muere_ahi(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from vp_model import series_characterization as sc

        monkeypatch.setattr(sc, "acorr_ljungbox", _avisa_y_delega(sc.acorr_ljungbox))
        registro, valor = _capturado(sc.ljung_box_pvalue, _serie_sintetica())
        assert AJENO in _mensajes(registro)
        assert 0.0 <= valor <= 1.0

    def test_control_benigno_ljung_box_real_no_emite_nada(self) -> None:
        """Medido: Ljung-Box no avisa en ninguna serie del panel; por eso la supresión se retiró entera."""
        from vp_model import series_characterization as sc

        serie = _serie_sintetica()
        registro, valor = _capturado(lambda: sc.ljung_box_pvalue(serie))
        assert _mensajes(registro) == [] and 0.0 <= valor <= 1.0


# ===========================================================================================
# 5/9 · vp_model/series_characterization.py:258 — advanced / Zivot-Andrews
# ===========================================================================================
@MODELADO
class TestCaracterizacionZivotAndrews:
    @staticmethod
    def _preparado(monkeypatch: pytest.MonkeyPatch):
        from vp_model import series_characterization as sc

        monkeypatch.setattr(sc, "_clean", lambda *a, **k: _serie_sintetica(80))
        return sc

    def test_red_otro_aviso_de_zivot_andrews_ya_no_muere_ahi(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import statsmodels.tsa.stattools as st

        sc = self._preparado(monkeypatch)
        monkeypatch.setattr(st, "zivot_andrews", _avisa_y_delega(st.zivot_andrews))
        registro, valor = _capturado(sc.advanced, "mexico", "EB2", "FAD")
        assert AJENO in _mensajes(registro)
        assert valor.country == "mexico"

    def test_control_benigno_el_sqrt_invalido_sigue_silenciado_y_el_p_sigue_siendo_nan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lo que se silencia es el AVISO de numpy, no el resultado: el p no finito se publica igual."""
        import numpy as np
        import statsmodels.tsa.stattools as st

        sc = self._preparado(monkeypatch)

        def za_no_finito(*args, **kwargs):
            warnings.warn("invalid value encountered in sqrt", RuntimeWarning, stacklevel=2)
            return (np.nan, np.nan, {}, np.nan, np.nan)

        monkeypatch.setattr(st, "zivot_andrews", za_no_finito)
        registro, valor = _capturado(sc.advanced, "india", "EB5_UNRESERVED", "FAD")
        assert _mensajes(registro) == []
        assert valor.za_break_pvalue != valor.za_break_pvalue  # NaN: la convención declarada de esta feature

    def test_red_un_runtimewarning_distinto_de_numpy_si_atraviesa(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """El filtro es por MENSAJE: otro `RuntimeWarning` de la misma llamada no queda cubierto."""
        import statsmodels.tsa.stattools as st

        sc = self._preparado(monkeypatch)
        otro = "divide by zero encountered in log"
        monkeypatch.setattr(st, "zivot_andrews", _avisa_y_delega(st.zivot_andrews, otro, RuntimeWarning))
        registro, _ = _capturado(sc.advanced, "mexico", "EB2", "FAD")
        assert otro in _mensajes(registro)


# ===========================================================================================
# 7/9 · experiments/build_eda_facts.py:116 — _formal_tests (supresión retirada entera)
# ===========================================================================================
@MODELADO
class TestBuildEdaFacts:
    @staticmethod
    def _censo_de_una_serie():
        import pandas as pd

        serie = _serie_sintetica(60)
        panel = pd.DataFrame(
            {
                "country": "mexico",
                "block": "employment",
                "category": "EB2",
                "table": "FAD",
                "status": "F",
                "bulletin_date": serie.index,
                "days_since_base": serie.to_numpy(),
            }
        )
        censo = pd.DataFrame(
            [{"country": "mexico", "block": "employment", "category": "EB2", "table": "FAD", "evaluable": True}]
        )
        return panel, censo

    def test_red_un_aviso_de_ljung_box_o_arch_ya_no_muere_ahi(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import statsmodels.stats.diagnostic as diag

        from experiments import build_eda_facts as bef

        monkeypatch.setattr(diag, "het_arch", _avisa_y_delega(diag.het_arch))
        panel, censo = self._censo_de_una_serie()
        registro, salida = _capturado(bef._formal_tests, panel, censo)
        assert AJENO in _mensajes(registro)
        assert len(salida) == 1 and salida.iloc[0]["verdict"] != "failed"

    def test_control_benigno_los_tests_formales_reales_no_emiten_nada(self) -> None:
        from experiments import build_eda_facts as bef

        panel, censo = self._censo_de_una_serie()
        registro, salida = _capturado(lambda: bef._formal_tests(panel, censo))
        assert _mensajes(registro) == []
        assert len(salida) == 1 and salida.iloc[0]["verdict"] != "failed"


# ===========================================================================================
# 6/9, 8/9 y 9/9 · los tres corredores de ajuste
# ===========================================================================================
@MODELADO
class TestCorredoresDeAjuste:
    """Cada corredor se detiene en su primera llamada; lo que se mide es qué avisos escapan."""

    @staticmethod
    def _corre(modulo, objetivo: str, mensaje: str, categoria: type[Warning], monkeypatch, entrada, *args):
        blanco = modulo
        for parte in objetivo.split(".")[:-1]:
            blanco = getattr(blanco, parte)
        monkeypatch.setattr(
            blanco, objetivo.split(".")[-1], _avisa_y_revienta(mensaje, categoria, _Corte), raising=True
        )
        with warnings.catch_warnings(record=True) as registro:
            warnings.simplefilter("always")
            with pytest.raises(_Corte):
                entrada(*args)
        return _mensajes(list(registro))

    def test_red_auto_arima_deja_pasar_un_aviso_ajeno(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """La envoltura de `run()` no se traga lo ajeno.

        ⚠️ Esta prueba NO discrimina contra el código anterior, y se deja dicho: la supresión de
        `auto_arima_baseline` era de NIVEL DE MÓDULO, y el `simplefilter("always")` de este arnés
        la anula al entrar. El daño real de un filtro instalado al importar es que contamina el
        estado global de quien importe, y eso lo mide
        `test_importar_un_corredor_no_instala_un_filtro_universal`, que sí cae contra
        `main@a8029f2`.
        """
        from experiments import auto_arima_baseline as aab

        salida = self._corre(aab, "dataset.list_series", AJENO, _Ajeno, monkeypatch, aab.run)
        assert AJENO in salida

    def test_control_benigno_auto_arima_calla_los_avisos_de_ajuste(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from experiments import auto_arima_baseline as aab

        salida = self._corre(aab, "dataset.list_series", known_fit_warnings()[0], UserWarning, monkeypatch, aab.run)
        assert salida == []

    def test_red_freeze_shadow_deja_pasar_un_aviso_ajeno(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from experiments import freeze_shadow as fs

        salida = self._corre(fs, "config.seed_everything", AJENO, _Ajeno, monkeypatch, fs.main)
        assert AJENO in salida

    def test_control_benigno_freeze_shadow_calla_los_avisos_de_ajuste(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from experiments import freeze_shadow as fs

        salida = self._corre(
            fs,
            "config.seed_everything",
            known_fit_warnings()[2],
            UserWarning,
            monkeypatch,
            fs.main,
        )
        assert salida == []

    def test_red_generate_web_forecasts_deja_pasar_un_aviso_ajeno(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from experiments import generate_web_forecasts as gwf

        salida = self._corre(gwf, "config.seed_everything", AJENO, _Ajeno, monkeypatch, gwf.run)
        assert AJENO in salida

    def test_control_benigno_generate_web_forecasts_calla_los_avisos_de_ajuste(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from experiments import generate_web_forecasts as gwf

        salida = self._corre(
            gwf,
            "config.seed_everything",
            known_fit_warnings()[3],
            UserWarning,
            monkeypatch,
            gwf.run,
        )
        assert salida == []


# ===========================================================================================
# Estructural: que no vuelvan, y que el import no tenga efectos laterales
# ===========================================================================================
CORREDORES = ("auto_arima_baseline", "freeze_shadow", "generate_web_forecasts")


@pytest.mark.parametrize("modulo", CORREDORES)
def test_el_cuerpo_del_corredor_vive_dentro_de_la_envoltura(modulo: str) -> None:
    """El punto de entrada abre `silenced_fit_warnings()` y TODO su cuerpo cuelga de ahí."""
    entrada = {"auto_arima_baseline": "run", "freeze_shadow": "main", "generate_web_forecasts": "run"}[modulo]
    arbol = ast.parse((RAIZ / "experiments" / f"{modulo}.py").read_text(encoding="utf-8"))
    funcion = next(
        n for n in ast.walk(arbol) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == entrada
    )
    cuerpo = [n for n in funcion.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    assert len(cuerpo) == 1 and isinstance(cuerpo[0], ast.With), f"{modulo}.{entrada} no está envuelto por completo"
    llamadas = [
        i.context_expr.func.id
        for i in cuerpo[0].items
        if isinstance(i.context_expr, ast.Call) and isinstance(i.context_expr.func, ast.Name)
    ]
    assert "silenced_fit_warnings" in llamadas


@pytest.mark.parametrize("modulo", CORREDORES)
def test_ningun_corredor_instala_filtros_al_importarse(modulo: str) -> None:
    """Un filtro a nivel de módulo contamina a QUIEN lo importe: por eso los tres lo perdieron."""
    arbol = ast.parse((RAIZ / "experiments" / f"{modulo}.py").read_text(encoding="utf-8"))
    for nodo in arbol.body:
        for sub in ast.walk(nodo):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                assert sub.func.attr not in {"filterwarnings", "simplefilter"}, (
                    f"{modulo} instala un filtro al importarse (línea {sub.lineno})"
                )


@MODELADO  # el subproceso importa el corredor, y los tres arrastran `darts` (H9)
@pytest.mark.parametrize("modulo", CORREDORES)
def test_importar_un_corredor_no_instala_un_filtro_universal(modulo: str) -> None:
    """CONDUCTUAL: importar el corredor no deja instalado un filtro que se trague TODO.

    ⚠️ No se exige que ``warnings.filters`` quede idéntico: medido, importar cualquiera de los
    tres arrastra **21 filtros de terceros** (torch, lightning, numpy, urllib3, statsmodels,
    scipy, requests), todos acotados por mensaje o por categoría concreta. Exigir igualdad sería
    probar el ecosistema, no este repositorio. Lo que sí se exige es que no aparezca la firma
    exacta que instalaba ``filterwarnings("ignore")``: acción ``ignore``, sin mensaje, categoría
    raíz ``Warning`` y sin módulo. Contra ``main@a8029f2`` esto falla para
    ``auto_arima_baseline``, que la instalaba en el proceso de cualquiera que lo importase.

    Se mide en un subproceso limpio porque en éste los tres ya están importados.
    """
    import subprocess
    import sys

    # ⚠️ Códigos DISTINTOS para causas distintas: antes, un `ModuleNotFoundError` del subproceso
    # se reportaba como «instaló un filtro universal», que es un diagnóstico falso. Es la sexta
    # reincidencia de la clase M14 y la que rompía el job base (H9 de la auditoría ciega).
    guion = (
        "import warnings, importlib, sys\n"
        "try:\n"
        f"    importlib.import_module('experiments.{modulo}')\n"
        "except ModuleNotFoundError as exc:\n"
        "    print(f'IMPORT_FALLIDO {exc}')\n"
        "    sys.exit(2)\n"
        "universal = [f for f in warnings.filters\n"
        "             if f[0] == 'ignore' and f[1] is None and f[2] is Warning and f[3] is None]\n"
        "print(len(universal))\n"
        "sys.exit(1 if universal else 0)\n"
    )
    fin = subprocess.run([sys.executable, "-c", guion], cwd=RAIZ, capture_output=True, text=True, timeout=600)
    assert fin.returncode != 2, (
        f"el subproceso no pudo importar experiments.{modulo}: {fin.stdout.strip()}. "
        "Esta prueba necesita el extra `model` y por eso lleva la marca MODELADO"
    )
    assert fin.returncode == 0, (
        f"importar experiments.{modulo} instaló un filtro universal "
        f"({fin.stdout.strip()} encontrados)\n{fin.stderr[-2000:]}"
    )


def test_el_inventario_de_supresiones_amplias_esta_en_cero() -> None:
    """La deuda diferida de D2-C queda cerrada, y se comprueba leyendo el CÓDIGO."""
    from tools import check_warnings as cw

    assert cw.detect_broad_suppressions(RAIZ) == []
