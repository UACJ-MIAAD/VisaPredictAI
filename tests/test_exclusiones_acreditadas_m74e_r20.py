"""M74-E-R20 · una (modelo, serie) que no se puede calcular se excluye SÓLO si la etapa 1 ya falló ahí.

La segunda campaña real sobre `9b9489f` (`rederiv_9b9489f_20260916T151013`, 17-sep-2026) completó la etapa 1 en
12 h 38 min y la 3 en 7 min, y murió en la 3.5: SARIMA no converge en India F2B FAD (`LinAlgError: LU decomposition
error`) y `persist_forecasts._rows` abortaba la reconstrucción entera ante la primera combinación no calculable,
mientras la etapa 1 de la MISMA campaña había dejado esa fila del pool sin `hold_mase` y había seguido. Dos etapas,
dos definiciones de «completo»: 14 combinaciones SARIMA (2 FAD, 12 DFF) hacían imposible pasar la 3.5 en este panel.

Ahora: la exclusión se admite sólo si el pool de la etapa 1 de esta campaña (`campaign_pool_{table}_family.csv`,
misma `run_id`) no tiene `hold_mase` finito para la combinación; queda en el recibo con su error; el conjunto
esperado se acredita sin sus claves; y el lector re-deriva los fallos de la etapa 1 y exige igualdad exacta.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from tests.holdout_fixture import escena_coherente, head_vivo, panel_sha  # noqa: E402
from vp_model import persist_forecasts as pf  # noqa: E402

pytest.importorskip("darts")

CAMP = "camp_r20"
ROTA = ("sarima", "india", "F2B")  # la combinación real que mató la campaña del 17-sep


def _pool(
    reports: Path,
    *,
    run_id: str = CAMP,
    sin_metricas: frozenset[tuple[str, str, str]] | set[tuple[str, str, str]] = frozenset(),
) -> Path:
    """Un pool de la etapa 1 con una fila por (modelo del pool × serie del catálogo FAD family)."""
    from vp_model import dataset

    ruta = reports / "campaign" / "campaign_pool_FAD_family.csv"
    ruta.parent.mkdir(parents=True, exist_ok=True)
    lineas = ["run_id,model,country,category,table,sel_mase,hold_mase,secs"]
    for r in dataset.list_series(table="FAD", block="family").itertuples():
        for m in pf.HOLDOUT_POOL_MODELS:
            mase = "" if (m, r.country, r.category) in sin_metricas else "0.1"
            lineas.append(f"{run_id},{m},{r.country},{r.category},FAD,{mase},{mase},1.0")
    ruta.write_text("\n".join(lineas) + "\n", encoding="utf-8")
    return ruta


def _walkforward_falso(monkeypatch: pytest.MonkeyPatch, rotas: set[tuple[str, str, str]]) -> None:
    """El walk-forward REAL tarda horas; aquí la serie real es su propio pronóstico y `rotas` revienta como SARIMA."""

    def run_forecasts(m: str, country: str, category: str, table: str, model: object | None = None):
        if (m, country, category) in rotas:
            raise np.linalg.LinAlgError("LU decomposition error.")
        ts = pf._grid(country, category, table)
        return ts, ts

    monkeypatch.setattr(pf.walkforward, "run_forecasts", run_forecasts)


def _reconstruir(reports: Path) -> Path:
    return pf.rebuild("FAD", campaign_id=CAMP, code_sha=head_vivo(), panel_sha256=panel_sha(), reports=reports)


def test_RED_una_combinacion_que_la_etapa_1_dejo_sin_metricas_se_excluye_y_se_acredita(tmp_path, monkeypatch) -> None:
    """Contra `9b9489f`: `rebuild` aborta con HoldoutForecastsError en la primera combinación rota."""
    reports = escena_coherente(tmp_path, monkeypatch, campaign_id=CAMP)
    _pool(reports, sin_metricas={ROTA})
    _walkforward_falso(monkeypatch, {ROTA})
    destino = _reconstruir(reports)
    acta = json.loads(Path(str(destino) + ".receipt.json").read_text(encoding="utf-8"))
    assert [(e["model"], e["country"], e["category"]) for e in acta["excluded"]] == [ROTA]
    assert acta["excluded"][0]["error"].startswith("LinAlgError")
    esperado = pf.expected_keys("FAD")
    fuera = {k for k in esperado if (k[0], k[1], k[2]) == ROTA}
    assert fuera and acta["n_expected_keys"] == len(esperado) - len(fuera)
    marco = pf.read_accredited("FAD", reports=reports)  # el lector re-deriva los fallos de la etapa 1 y acepta
    assert len(marco) == len(esperado) - len(fuera)
    assert marco[(marco.model == "sarima") & (marco.country == "india") & (marco.category == "F2B")].empty


def test_un_fallo_que_la_etapa_1_si_calculo_sigue_abortando(tmp_path, monkeypatch) -> None:
    """Fail-closed intacto: sin veredicto de la etapa 1 no hay exclusión, hay fallo."""
    reports = escena_coherente(tmp_path, monkeypatch, campaign_id=CAMP)
    _pool(reports)  # todas con hold_mase finito
    _walkforward_falso(monkeypatch, {ROTA})
    with pytest.raises(pf.HoldoutForecastsError, match="SÍ lo calculó"):
        _reconstruir(reports)


def test_sin_pool_de_la_etapa_1_no_hay_exclusion_posible(tmp_path, monkeypatch) -> None:
    reports = escena_coherente(tmp_path, monkeypatch, campaign_id=CAMP)
    _walkforward_falso(monkeypatch, {ROTA})
    with pytest.raises(pf.HoldoutForecastsError, match="sin pool de la etapa 1"):
        _reconstruir(reports)


def test_un_pool_de_OTRA_corrida_no_justifica_la_exclusion(tmp_path, monkeypatch) -> None:
    reports = escena_coherente(tmp_path, monkeypatch, campaign_id=CAMP)
    _pool(reports, run_id="otra_campana", sin_metricas={ROTA})
    _walkforward_falso(monkeypatch, {ROTA})
    with pytest.raises(pf.HoldoutForecastsError, match="exige exactamente una"):
        _reconstruir(reports)


def test_RED_el_lector_rechaza_una_exclusion_que_la_etapa_1_no_respalda(tmp_path, monkeypatch) -> None:
    """El recibo es impecable y el CSV cuadra con él; lo que cambia después es el veredicto de la etapa 1."""
    reports = escena_coherente(tmp_path, monkeypatch, campaign_id=CAMP)
    _pool(reports, sin_metricas={ROTA})
    _walkforward_falso(monkeypatch, {ROTA})
    _reconstruir(reports)
    _pool(reports)  # alguien «arregla» el pool: ahora la etapa 1 dice que SARIMA sí se calculó
    with pytest.raises(pf.HoldoutForecastsError, match="no son los fallos de la etapa 1"):
        pf.read_accredited("FAD", reports=reports)


def test_RED_el_lector_rechaza_un_fallo_de_la_etapa_1_que_el_recibo_no_excluye(tmp_path, monkeypatch) -> None:
    """La otra dirección: la etapa 1 falló en dos combinaciones y el productor «calculó» una de ellas."""
    reports = escena_coherente(tmp_path, monkeypatch, campaign_id=CAMP)
    otra = ("sarima", "india", "F3")
    _pool(reports, sin_metricas={ROTA})
    _walkforward_falso(monkeypatch, {ROTA})
    _reconstruir(reports)
    _pool(reports, sin_metricas={ROTA, otra})
    with pytest.raises(pf.HoldoutForecastsError, match="faltan"):
        pf.read_accredited("FAD", reports=reports)


@pytest.mark.parametrize(
    "mutacion",
    [
        lambda ex: ex.pop("error"),
        lambda ex: ex.update({"error": ""}),
        lambda ex: ex.update({"model": "no_existe"}),
    ],
)
def test_un_recibo_con_exclusiones_malformadas_no_acredita(tmp_path, monkeypatch, mutacion) -> None:
    reports = escena_coherente(tmp_path, monkeypatch, campaign_id=CAMP)
    _pool(reports, sin_metricas={ROTA})
    _walkforward_falso(monkeypatch, {ROTA})
    destino = _reconstruir(reports)
    recibo = Path(str(destino) + ".receipt.json")
    acta = json.loads(recibo.read_text(encoding="utf-8"))
    mutacion(acta["excluded"][0])
    recibo.write_text(json.dumps(acta), encoding="utf-8")
    with pytest.raises(pf.HoldoutForecastsError, match="excluded|universo"):
        pf.read_accredited("FAD", reports=reports)


def test_control_sin_exclusiones_el_contrato_es_el_de_siempre(tmp_path, monkeypatch) -> None:
    """Sin fallos no se consulta el pool, el recibo declara `excluded: []` y se exige el universo entero."""
    reports = escena_coherente(tmp_path, monkeypatch, campaign_id=CAMP)
    _walkforward_falso(monkeypatch, set())
    destino = _reconstruir(reports)
    acta = json.loads(Path(str(destino) + ".receipt.json").read_text(encoding="utf-8"))
    assert acta["excluded"] == [] and acta["n_expected_keys"] == len(pf.expected_keys("FAD"))
    assert len(pf.read_accredited("FAD", reports=reports)) == acta["n_expected_keys"]
