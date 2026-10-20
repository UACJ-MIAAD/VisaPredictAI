"""M74-E · El transporte tiene PRODUCTOR, y el camino real lo ejecuta.

El defecto que cierra es mío. M74-E construyó la tubería entera del transporte —constructor de
filas, artefacto, recibo por conjunto exacto de claves, cuatro REDs adversariales, consumidor
fail-closed— y **nadie llamaba a `write()`**. Sus dos únicas referencias eran consumidores.

⚠️ **Y mis REDs pasaban igual**, porque ejercitaban `write()` y `load_and_accredit()` con datos
sintéticos: ninguno comprobaba que **alguien los invocara** en el camino real. Probar las piezas no
prueba la cadena; es la lección de M41-R1 aplicada tarde a mi propio módulo.

Estas pruebas son de **integración**: recorren productor → artefacto → recibo → consumidor. Contra
`1022c9d` fallan porque ahí el productor **no existe**.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "experiments"))

ORQUESTADOR = RAIZ / "experiments" / "save_finalists.sh"
PRODUCTOR = RAIZ / "experiments" / "persist_transported_forecasts.py"
CONSUMIDOR = RAIZ / "experiments" / "export_forecasts.py"


# ═══════════════════════════════ 1 · el camino real lo invoca, y ANTES del consumidor
def test_the_runbook_has_a_producer_at_all() -> None:
    """★ RED contra 1022c9d: allí este archivo no existe y el paso no está en el orquestador."""
    assert PRODUCTOR.is_file(), "sin productor, el transporte es código que nadie ejecuta"
    guion = ORQUESTADOR.read_text(encoding="utf-8")
    assert "persist_transported_forecasts.py" in guion, (
        "el orquestador no produce el transporte; el exportador fallaría cerrado buscando un "
        "artefacto que ninguna etapa escribe"
    )


def _lineas_ejecutables(p: Path) -> list[str]:
    """Las líneas que el shell ejecuta. ⚠️ Comparar contra el texto entero hace que un COMENTARIO
    que menciona un guion cuente como si lo invocara: mi primera versión de esta prueba falló así,
    porque la cabecera del orquestador nombra `export_forecasts.py` al explicar el fallo de julio."""
    return [ln for ln in p.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#")]


def test_the_producer_runs_before_its_only_consumer() -> None:
    """El orden ES la garantía: producir después de exportar no serviría de nada."""
    vivo = "\n".join(_lineas_ejecutables(ORQUESTADOR))
    i_prod = vivo.index("persist_transported_forecasts.py")
    i_cons = vivo.index("export_forecasts.py")
    assert i_prod < i_cons, "el productor tiene que correr ANTES del exportador"


def test_the_producer_refuses_to_run_without_a_campaign() -> None:
    """Un artefacto sin procedencia es peor que ninguno: correr suelto aborta."""
    fin = subprocess.run(
        [sys.executable, str(PRODUCTOR)],
        cwd=RAIZ,
        capture_output=True,
        text=True,
        timeout=300,
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "PYTHONPATH": str(RAIZ)},
    )
    assert fin.returncode != 0
    assert "CAMPAIGN_ID" in (fin.stderr + fin.stdout)


# ═══════════════════════════════ 2 · la cadena completa, hermética
class _Resultado:
    def __init__(self, filas):
        self.holdout_rows = filas


@pytest.fixture
def escenario(tmp_path: Path):
    """Campaña sellada + panel + repositorio git: el mundo que el acreditador va a leer."""
    import hashlib

    raiz = tmp_path / "repo"
    (raiz / "reports" / "campaign").mkdir(parents=True)
    (raiz / "reports" / "eval").mkdir(parents=True)
    (raiz / "data" / "processed").mkdir(parents=True)
    panel = raiz / "data" / "processed" / "visa_panel_long.csv"
    panel.write_text("country,category,table,value\nmexico,F1,FAD,1\n", encoding="utf-8")
    panel_sha = "sha256:" + hashlib.sha256(panel.read_bytes()).hexdigest()
    cid, sha = "camp_integracion", "a" * 40
    (raiz / "reports" / "campaign" / "campaign.json").write_text(
        json.dumps({"campaign_id": cid, "source_git_sha": sha, "panel_sha256": panel_sha, "status": "running"}),
        encoding="utf-8",
    )
    return raiz, cid, sha, panel_sha


def _filas_sinteticas(cid, sha, panel_sha, universo, modelos):
    """Lo que `walkforward.backtest` devolvería: 24 objetivos por (serie, modelo)."""
    import pandas as pd

    # ⚠️ Sin importar `vp_model.holdout_rows`: arrastra el extra de modelado y el job base sólo
    # instala `.[dev]`. Es la trampa que este repo ha pisado seis veces (M14, M48, M59, M62, M64,
    # M72) y que un guardián propio vigila. El origen se deriva aquí con la MISMA regla —periodo
    # mensual, no días—, que es justo lo que la prueba quiere fijar.
    def _origen(d):
        return (d.to_period("M") - 1).to_timestamp()

    objetivos = {c: pd.date_range("2023-01-01", periods=24, freq="MS") for c in universo}
    salida = {}
    for table, country, category in universo:
        for modelo in modelos:
            filas = []
            for d in objetivos[(table, country, category)]:
                filas.append(
                    {
                        "campaign_id": cid, "table": table, "country": country, "category": category,
                        "model": modelo, "origin": _origen(d).strftime("%Y-%m-%d"),
                        "target": d.strftime("%Y-%m-%d"), "h": 1, "y_pred": 101.0, "y_true": 100.0,
                        "observed": True, "mase_scale": 7.5, "code_sha": sha, "panel_sha256": panel_sha,
                    }
                )  # fmt: skip
            salida[(table, country, category, modelo)] = filas
    return salida, objetivos


def test_producer_then_consumer_round_trip(escenario) -> None:
    """★ La cadena entera: el productor escribe, y el consumidor RE-ACREDITA lo escrito.

    Usa la costura `backtest=` para no entrenar 25 series × 2 modelos; todo lo demás —artefacto,
    recibo, conjunto exacto de claves, re-acreditación— es el código de producción.
    """
    import persist_transported_forecasts as prod

    from vp_model import transported_forecasts as tf

    raiz, cid, sha, panel_sha = escenario
    universo = [("FAD", "mexico", "F1"), ("DFF", "mexico", "F1")]
    modelos = ("ets", "theta")
    sinteticas, objetivos = _filas_sinteticas(cid, sha, panel_sha, universo, modelos)

    def backtest_falso(modelo, country, category, table):
        return _Resultado(sinteticas[(table, country, category, modelo)])

    filas = prod.recoger_filas(universo, modelos, backtest=backtest_falso)
    assert len(filas) == len(universo) * len(modelos) * 24

    esperado = tf.expected_keys(
        campaign_id=cid,
        universe=universo,
        models=modelos,
        targets={c: [(f["origin"], f["target"]) for f in sinteticas[(c[0], c[1], c[2], "ets")]] for c in universo},
    )
    recibo = tf.write(filas, esperado, root=raiz, campaign_id=cid, code_sha=sha, panel_sha256=panel_sha)
    assert recibo.is_file()

    # ★ el consumidor vuelve a acreditar lo que el productor escribió
    leidas = tf.load_and_accredit(root=raiz, campaign_id=cid, code_sha=sha, panel_sha256=panel_sha, esperado=esperado)
    assert len(leidas) == len(filas)
    assert {f["model"] for f in leidas} == set(modelos)


def test_a_producer_that_skips_one_model_does_not_pass_the_gate(escenario) -> None:
    """Producir sólo `ets` deja a `theta` fuera: el conjunto exacto lo caza, un conteo no."""
    import persist_transported_forecasts as prod

    from vp_model import transported_forecasts as tf

    raiz, cid, sha, panel_sha = escenario
    universo = [("FAD", "mexico", "F1")]
    sinteticas, _ = _filas_sinteticas(cid, sha, panel_sha, universo, ("ets", "theta"))

    def solo_ets(modelo, country, category, table):
        return _Resultado(sinteticas[(table, country, category, modelo)])

    filas = prod.recoger_filas(universo, ("ets",), backtest=solo_ets)
    esperado = tf.expected_keys(
        campaign_id=cid, universe=universo, models=("ets", "theta"),
        targets={universo[0]: [(f["origin"], f["target"]) for f in sinteticas[(*universo[0], "ets")]]},
    )  # fmt: skip
    with pytest.raises(tf.TransportError, match="AUSENTE"):
        tf.write(filas, esperado, root=raiz, campaign_id=cid, code_sha=sha, panel_sha256=panel_sha)


def test_the_producer_refuses_empty_holdout_rows(escenario) -> None:
    """Si el walk-forward no devolvió filas, no se rellena ni se supone: aborta."""
    import persist_transported_forecasts as prod

    with pytest.raises(SystemExit, match="no devolvió filas"):
        prod.recoger_filas([("FAD", "mexico", "F1")], ("ets",), backtest=lambda *a: _Resultado([]))


# ═══════════════════════════════ 3 · el instrumento sigue sin tocar el cálculo
def test_backtest_still_carries_the_rows_it_needs_to_transport() -> None:
    """`BacktestResult` expone `holdout_rows`; sin eso el productor no tendría qué recoger."""
    fuente = (RAIZ / "vp_model" / "walkforward.py").read_text(encoding="utf-8")
    assert "holdout_rows" in fuente
    # y se construyen DESPUÉS de las métricas, que es lo que hace imposible que las alteren
    assert fuente.index("holdout = {**metrics.compute(") < fuente.index("holdout_rows=holdout_rows.build_rows(")


def test_the_exporter_has_no_fallback_to_retrain(escenario) -> None:
    """Sin transporte acreditado no hay exportación: ni archivo viejo, ni `retrain=True`."""
    import ast

    arbol = ast.parse(CONSUMIDOR.read_text(encoding="utf-8"))
    # ⚠️ Por AST, no por texto: la docstring del consumidor PROMETE «sin caer a retrain=True», y una
    # búsqueda literal casaba con esa promesa. Un inventario por texto acusa a la documentación
    # (lección de M72), y aquí me acusó a mí.
    llamadas = {n.func.id for n in ast.walk(arbol) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    llamadas |= {n.func.attr for n in ast.walk(arbol) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "load_and_accredit" in llamadas, "el exportador debe acreditar el transporte"
    reentrenos = [
        kw
        for n in ast.walk(arbol)
        if isinstance(n, ast.Call)
        for kw in n.keywords
        if kw.arg == "retrain" and isinstance(kw.value, ast.Constant) and kw.value.value is True
    ]
    assert not reentrenos, "el exportador no puede recaer en reajustar"
