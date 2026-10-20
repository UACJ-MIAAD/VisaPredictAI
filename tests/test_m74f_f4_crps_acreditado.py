"""M74-F · F4 consume el CRPS de referencia ACREDITADO o se niega (decisión del autor 13-oct-2026: rechazar).

`make_result_figures.fig_coverage_crps` leía `crps_fad.csv` en crudo: un CSV sin recibo, de otra añada o de una
transacción fallida acababa dibujado en el panel (b) de F4 como si fuera vigente. Ahora pasa por
`champion.baseline_crps_provenance`; si no sale `acreditado`, la figura falla con el motivo, y `main` lo comprueba
ANTES de generar ninguna figura (no deja F1–F3 regeneradas a medias).

Ninguna prueba escribe figuras: `_emit` y las demás figuras son dobles que sólo registran la llamada. El control
acreditado usa el mundo git real de C6 con el productor real dentro de una campaña.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

import tests.test_m74f_complemento_cableado as cab
from tests.test_m74f_crps_baseline_contrato import _campana_en_curso, _productor

pytest.importorskip("darts")

import make_result_figures as mrf
import run_crps_baseline as rcb

from vp_model import champion

mundo, _codigo_del_checkout = cab.mundo, cab._codigo_del_checkout
COBERTURA = "table,baseline_coverage,split_coverage,aci_coverage\nFAD,0.85,0.9,0.92\nDFF,0.84,0.88,0.9\n"


@pytest.fixture
def emitidas(monkeypatch) -> list[str]:
    """`_emit` doble (no escribe) y BiTCN fijo (su CRPS sale de otro artefacto)."""
    llamadas: list[str] = []

    def emitir(fig, nombre: str) -> None:  # como el real: registra y CIERRA la figura, sin escribir
        llamadas.append(nombre)
        mrf.plt.close(fig)

    monkeypatch.setattr(mrf, "_emit", emitir)
    monkeypatch.setattr(mrf, "_bitcn_crps", lambda table: 40.0)
    return llamadas


def _reports(raiz: Path, monkeypatch, crps: str | None) -> Path:
    rep = raiz / "reports"
    (rep / "eval").mkdir(parents=True, exist_ok=True)
    (rep / "eval" / "conformal_coverage.csv").write_text(COBERTURA)
    if crps is not None:
        (rep / "eval" / "crps_fad.csv").write_text(crps)
    monkeypatch.setattr(mrf, "REP", rep)
    return rep


# ═══════════════════════════════ RED de conducta: hoy se dibuja lo que haya en disco
def test_f4_rechaza_un_crps_sin_recibo(tmp_path: Path, monkeypatch, emitidas) -> None:
    _reports(tmp_path, monkeypatch, "model,country,category,crps\narima,mexico,F1,10\ndeepar,mexico,F1,8\n")
    with pytest.raises(SystemExit, match="heredado"):
        mrf.fig_coverage_crps()
    assert emitidas == [], "una cifra heredada no puede llegar al panel (b) de F4"


def test_f4_rechaza_si_no_hay_crps(tmp_path: Path, monkeypatch, emitidas) -> None:
    _reports(tmp_path, monkeypatch, None)
    with pytest.raises(SystemExit, match="ausente"):
        mrf.fig_coverage_crps()
    assert emitidas == []


def test_f4_main_rechaza_antes_de_generar_ninguna_figura(tmp_path: Path, monkeypatch) -> None:
    _reports(tmp_path, monkeypatch, "model,country,category,crps\narima,mexico,F1,10\n")
    generadas: list[str] = []
    for nombre in ("fig_ranking", "fig_forecast", "fig_multiseed", "fig_coverage_crps", "fig_backtest_grid",
                   "fig_cd_diagram", "fig_error_heatmap", "forecast_vs_actual_rows"):  # fmt: skip
        monkeypatch.setattr(mrf, nombre, lambda *a, _n=nombre, **k: generadas.append(_n))
    with pytest.raises(SystemExit, match="heredado"):
        mrf.main([])
    assert generadas == [], "F1–F3 no se regeneran si F4 no puede publicarse"


# ═══════════════════════════════ controles: el CRPS acreditado se dibuja tal cual
def test_f4_control_dibuja_el_crps_acreditado_por_la_campana(mundo: dict, monkeypatch, emitidas) -> None:
    _campana_en_curso(mundo, monkeypatch)
    _productor(monkeypatch, mundo)
    rcb.main([])
    rep = _reports(mundo["compl"], monkeypatch, crps=None)
    medias = champion.baseline_crps_provenance(reports=rep, panel=mundo["compl"] / cab.cx.PANEL_REL)["means"]
    dibujadas: list[float] = []
    barh = mrf.plt.Axes.barh

    def registrar(ax, y, ancho, *a, **k):
        dibujadas.extend(float(v) for v in ancho)
        return barh(ax, y, ancho, *a, **k)

    monkeypatch.setattr(mrf.plt.Axes, "barh", registrar)
    mrf.fig_coverage_crps()
    assert emitidas == ["results_coverage_crps"]
    assert sorted(dibujadas) == sorted([*(v for v in medias.values() if v is not None), 40.0])


def test_f4_main_pasa_la_raiz_de_la_madre_a_la_comprobacion_previa_y_a_la_figura(tmp_path: Path, monkeypatch) -> None:
    _reports(tmp_path, monkeypatch, "model,country,category,crps\narima,mexico,F1,10\n")
    vistas: list[object] = []

    def procedencia(**kw):
        vistas.append(kw.get("parent_root"))
        return {"status": "acreditado", "means": {"arima": 10.0}, "reason": None}

    monkeypatch.setattr(champion, "baseline_crps_provenance", procedencia)
    recibido: list[Any] = []
    for nombre in ("fig_ranking", "fig_forecast", "fig_multiseed", "fig_backtest_grid", "fig_cd_diagram",
                   "fig_error_heatmap"):  # fmt: skip
        monkeypatch.setattr(mrf, nombre, lambda *a, **k: None)
    monkeypatch.setattr(mrf, "forecast_vs_actual_rows", lambda t: mrf.pd.DataFrame())
    monkeypatch.setattr(mrf, "fig_coverage_crps", lambda *a, **k: recibido.append((a, k)))
    (tmp_path / "reports" / "prospective").mkdir()
    mrf.main(["--crps-parent-root", str(tmp_path / "madre")])
    assert vistas == [tmp_path / "madre"], "la comprobación previa acredita con la raíz de la madre"
    assert recibido == [((tmp_path / "madre",), {})], "y la figura vuelve a acreditar con la misma raíz"


def test_f4_los_modelos_sin_ninguna_serie_puntuada_no_se_dibujan(tmp_path: Path, monkeypatch) -> None:
    _reports(tmp_path, monkeypatch, None)
    monkeypatch.setattr(champion, "baseline_crps_provenance",
                        lambda **kw: {"status": "acreditado", "means": {"arima": 10.0, "deepar": None}, "reason": None})  # fmt: skip
    assert mrf._crps_referencia().to_dict() == {"arima": 10.0}


# ═══════════════════════════════ auditoría F4: ninguna vía pública salta la acreditación
def test_f4_no_hay_via_publica_que_salte_la_acreditacion(tmp_path: Path, monkeypatch, emitidas) -> None:
    """`fig_coverage_crps(crps=...)` dibujaba lo que le pasaran sin CSV ni recibo (CRPS −123 incluido)."""
    _reports(tmp_path, monkeypatch, None)
    figura: Any = mrf.fig_coverage_crps  # la llamada adversarial es inválida a propósito
    with pytest.raises((SystemExit, TypeError)):
        figura(crps=mrf.pd.Series({"arima": -123.0}))
    assert emitidas == []
    assert list(inspect.signature(mrf.fig_coverage_crps).parameters) == ["crps_parent_root"]
