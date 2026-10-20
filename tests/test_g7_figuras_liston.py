"""M74-F · G7 · B1/B2 — figuras de resultados: el listón en la MISMA métrica y universo que lo dibujado.

RED (auditoría 15-oct sobre `3f62bed`, reproducida en la reconciliación real):

* `_liston('FAD')` devolvía `fad_champion_mase`, una MASE de **selección**, y F1 (ranking) y F3 (multi-semilla)
  la dibujaban sobre ejes de MASE de **reservado**: con la campaña causal, AutoBiTCN (0.162) aparecía muy por
  encima del «listón» 0.114 cuando en reservado está por debajo de ETS/Theta (0.167/0.171).
* F3 dibujaba un «oráculo de selección» FIJO (0.113) de una corrida vieja, sin artefacto que lo respalde.
* El CD del paper y la figura de hold-out sólo se producían con efectos laterales (reescribir
  `significance_summary.json` dentro de una campaña `running`; reescribir una tabla `.tex`).

Las pruebas inspeccionan lo que la figura DIBUJA (líneas, puntos, etiquetas) y lo que el productor ESCRIBE.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd
import pytest

matplotlib.use("Agg")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments"))

# `vp_model.report` y las figuras arrastran el extra de modelado; el job base no lo instala.
pytest.importorskip("darts")

import make_result_figures as mrf
import significance_tables as st

from vp_model import report

SERIES = [("mexico", "F1"), ("india", "F2A"), ("china", "F3")]
#: Reservado (hold) y selección (sel) por modelo: la selección de ETS/Theta es MUY distinta del reservado.
HOLD = {"FAD": {"ets": [0.16, 0.18, 0.40], "theta": [0.17, 0.17, 0.10], "naive1": [0.15, 0.16, 0.20]},
        "DFF": {"ets": [0.12, 0.14, 0.30], "theta": [0.12, 0.13, 0.05], "naive1": [0.12, 0.13, 0.20]}}  # fmt: skip
SEL = 0.05


def _pool(rep: Path) -> None:
    (rep / "eval").mkdir(parents=True, exist_ok=True)
    for t, modelos in HOLD.items():
        filas = [{"run_id": "r1", "model": m, "country": c, "category": k, "table": t, "sel_mase": SEL, "hold_mase": v}
                 for m, vals in modelos.items() for (c, k), v in zip(SERIES, vals, strict=True)]  # fmt: skip
        pd.DataFrame(filas).to_csv(rep / "eval" / f"model_comparison_{t}21.csv", index=False)


@pytest.fixture
def figuras(tmp_path: Path, monkeypatch) -> dict:
    """REP temporal con los pools; `_emit` captura la figura sin escribirla; el `_KF` viejo apunta a selección."""
    rep = tmp_path / "reports"
    _pool(rep)
    monkeypatch.setattr(mrf, "REP", rep)
    monkeypatch.setattr(mrf, "_KF", {"fad_champion_mase": SEL, "ets_dff_mean": SEL}, raising=False)
    capturadas: dict = {}
    monkeypatch.setattr(mrf, "_emit", lambda fig, nombre: capturadas.__setitem__(nombre, fig))
    return capturadas


def _media(t: str, modelo: str, idx: list[int]) -> float:
    return sum(HOLD[t][modelo][i] for i in idx) / len(idx)


def _lineas_verticales(ax) -> list[float]:
    return [float(ln.get_xdata()[0]) for ln in ax.lines if len(set(map(float, ln.get_xdata()))) == 1]


# ═══════════════════════════════ B1 · F1 ranking
def test_f1_el_liston_es_la_mejor_mase_de_reservado_de_etsx_theta_en_su_universo(figuras: dict) -> None:
    mrf.fig_ranking()
    fig = figuras["results_ranking_mase"]
    esperado = {"FAD": min(_media("FAD", "ets", [0, 1, 2]), _media("FAD", "theta", [0, 1, 2])),
                "DFF": _media("DFF", "ets", [0, 1, 2])}  # fmt: skip
    for ax, t in zip(fig.axes, ("FAD", "DFF"), strict=True):
        assert _lineas_verticales(ax) == [pytest.approx(esperado[t])], (t, _lineas_verticales(ax))
        textos = [x.get_text() for x in ax.texts if "listón" in x.get_text()]
        assert len(textos) == 1 and "reservado" in textos[0] and f"{esperado[t]:.3f}" in textos[0], textos


# ═══════════════════════════════ B1-bis · F1: sólo se ranquea lo que cubre el universo del listón
# RED (auditoría 16-oct sobre `c964881`): en DFF real ARIMA puntúa 23 de 25 series (0.1199) y se dibujaba unido al
# listón de ETS sobre 25 (0.1286), aunque ETS sobre esas mismas 23 series da 0.1149: la comparación cambia de signo.
def _con_parciales(rep: Path, tabla: str, cobertura: dict[str, list[int]], valor: float = 0.01) -> None:
    """Añade modelos que sólo puntúan las series ``cobertura[modelo]`` (NaN en el resto), con un error MUY bajo."""
    pool = rep / "eval" / f"model_comparison_{tabla}21.csv"
    d = pd.read_csv(pool)
    filas = [{"run_id": "r1", "model": m, "country": c, "category": k, "table": tabla, "sel_mase": SEL,
              "hold_mase": valor if i in idx else float("nan")}
             for m, idx in cobertura.items() for i, (c, k) in enumerate(SERIES)]  # fmt: skip
    pd.concat([d, pd.DataFrame(filas)], ignore_index=True).to_csv(pool, index=False)


def _textos(ax) -> str:
    return " | ".join([x.get_text() for x in ax.texts] + [ax.get_xlabel(), ax.get_title()])


@pytest.fixture
def parciales(figuras: dict) -> dict:
    for t in ("FAD", "DFF"):
        _con_parciales(mrf.REP, t, {"arima": [0, 1], "arima_lstm": []})
    mrf.fig_ranking()
    return figuras


def test_f1_un_modelo_de_cobertura_parcial_no_se_dibuja_contra_el_liston(parciales: dict) -> None:
    for ax, t in zip(parciales["results_ranking_mase"].axes, ("FAD", "DFF"), strict=True):
        etiquetas = {x.get_text() for x in ax.get_yticklabels()}
        assert etiquetas == {"ets", "theta", "naive1"}, (t, etiquetas)
        puntos = sorted(p for v in _puntos(ax).values() for p in v)
        assert puntos == [
            pytest.approx(_media(t, m, [0, 1, 2])) for m in sorted(HOLD[t], key=lambda m: _media(t, m, [0, 1, 2]))
        ], (t, puntos)
        (segmentos,) = [c.get_segments() for c in ax.collections if hasattr(c, "get_segments")]
        liston = _lineas_verticales(ax)[0]
        assert len(segmentos) == 3 and all(s[0][0] == pytest.approx(liston) for s in segmentos), (t, segmentos)


def test_f1_declara_aparte_los_parciales_con_su_cobertura(parciales: dict) -> None:
    for ax, t in zip(parciales["results_ranking_mase"].axes, ("FAD", "DFF"), strict=True):
        texto = _textos(ax)
        assert "arima 2/3" in texto and "arima_lstm 0/3" in texto and "parcial" in texto, (t, texto)


def test_f1_el_titulo_dice_el_universo_medido(parciales: dict) -> None:
    for ax, t in zip(parciales["results_ranking_mase"].axes, ("FAD", "DFF"), strict=True):
        assert "3 series" in ax.get_title() and "25" not in ax.get_title(), (t, ax.get_title())


# ═══════════════════════════════ B1 · F3 multi-semilla
def _semillas(universo: list[int], monkeypatch, *, otra: list[int] | None = None) -> None:
    """Cinco semillas con MASE por serie sobre ``universo`` (índices de SERIES); ``otra`` = universo de la 5.ª."""

    def puntuar(pattern, col, table):
        out = []
        for s in range(5):
            idx = otra if (otra is not None and s == 4) else universo
            out.append(pd.Series({SERIES[i]: 0.10 + 0.001 * s for i in idx}, name=col))
        return out

    monkeypatch.setattr(mrf, "_seed_scores", puntuar, raising=False)
    monkeypatch.setattr(mrf, "_seed_mases", lambda p, c, t: [float(x.mean()) for x in puntuar(p, c, t)], raising=False)


def _puntos(ax) -> dict[str, list[float]]:
    """Posición x de cada marcador de dispersión (diamantes del listón, cuadrados del oráculo) por etiqueta."""
    from matplotlib.collections import PathCollection

    out: dict[str, list[float]] = {}
    for col in ax.collections:
        if isinstance(col, PathCollection):
            out.setdefault(str(col.get_label()), []).extend(float(x) for x in np.asarray(col.get_offsets())[:, 0])
    return out


def test_f3_el_liston_se_calcula_sobre_el_universo_que_puntua_el_profundo(figuras: dict, monkeypatch) -> None:
    _semillas([0, 1], monkeypatch)  # el profundo sólo puntúa dos de las tres series
    mrf.fig_multiseed()
    ax = figuras["results_multiseed_ci"].axes[0]
    puntos = _puntos(ax)
    etiqueta = [lbl for lbl in puntos if "Listón" in lbl]
    assert len(etiqueta) == 1 and "reservado" in etiqueta[0] and "n=2" in etiqueta[0], list(puntos)
    esperado = sorted([min(_media("FAD", "ets", [0, 1]), _media("FAD", "theta", [0, 1])), _media("DFF", "ets", [0, 1])])
    dibujado = sorted(p for v in puntos.values() for p in v)
    assert dibujado == [pytest.approx(e) for e in esperado], (dibujado, esperado)


def test_f3_no_dibuja_un_oraculo_fijo(figuras: dict, monkeypatch) -> None:
    _semillas([0, 1, 2], monkeypatch)
    mrf.fig_multiseed()
    ax = figuras["results_multiseed_ci"].axes[0]
    etiquetas = ax.get_legend_handles_labels()[1]
    assert not any("ráculo" in e for e in etiquetas), etiquetas
    assert not any(p == pytest.approx(0.113) for v in _puntos(ax).values() for p in v)


def test_f3_semillas_con_universos_distintos_no_dibujan(figuras: dict, monkeypatch) -> None:
    _semillas([0, 1, 2], monkeypatch, otra=[0, 1])
    with pytest.raises(SystemExit, match="universo"):
        mrf.fig_multiseed()
    assert "results_multiseed_ci" not in figuras


def test_un_liston_que_no_cubre_el_universo_no_se_promedia_en_silencio(figuras: dict) -> None:
    pool = mrf.REP / "eval" / "model_comparison_FAD21.csv"
    d = pd.read_csv(pool)
    d.loc[(d.model == "theta") & (d.country == "china"), "hold_mase"] = float("nan")
    d.to_csv(pool, index=False)
    with pytest.raises(SystemExit, match="theta"):
        mrf.fig_ranking()


# ═══════════════════════════════ B2 · CD del paper sin efectos laterales
def _pool_campana(rep: Path, *, alterar: bool = False) -> None:
    """15 series × 4 modelos con pérdidas deterministas y variadas (el MCS de `arch` no admite muestras mínimas)."""
    (rep / "campaign").mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(7)
    filas = [{"country": f"pais{i}", "category": "F1", "model": m, "hold_mase": round(0.1 + 0.03 * j + rng.random() * 0.05, 4)}
             for i in range(15) for j, m in enumerate(("naive1", "ets", "theta", "drift"))]  # fmt: skip
    if alterar:
        filas[0]["hold_mase"] = 0.9
    pd.DataFrame(filas).to_csv(rep / "campaign" / "campaign_pool_FAD_family.csv", index=False)


def _resumen_sellado(rep: Path) -> Path:
    r = st._ranking("FAD")
    resumen = {"ranking": {"FAD": {k: v for k, v in r.items() if not k.startswith("_")}}, "dm": {}}
    ruta = rep / "eval" / "significance_summary.json"
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(resumen, indent=2, default=str) + "\n")
    return ruta


@pytest.fixture
def cd(tmp_path: Path, monkeypatch) -> dict:
    pytest.importorskip("scikit_posthocs")
    rep = tmp_path / "reports"
    monkeypatch.setattr(st, "REPORTS", rep)
    monkeypatch.setattr(st, "FIGS", tmp_path / "paper" / "Figures")
    _pool_campana(rep)
    return {"rep": rep, "fig": tmp_path / "paper" / "Figures" / "fig_cd_diagram.pdf"}


def test_cd_only_dibuja_sin_tocar_el_resumen_sellado(cd: dict) -> None:
    resumen = _resumen_sellado(cd["rep"])
    antes = resumen.read_bytes()
    st.main(["--cd-only"])
    assert cd["fig"].is_file() and resumen.read_bytes() == antes


def test_cd_only_se_niega_si_su_ranking_no_es_el_sellado(cd: dict) -> None:
    _resumen_sellado(cd["rep"])
    _pool_campana(cd["rep"], alterar=True)
    with pytest.raises(SystemExit, match="no es el sellado"):
        st.main(["--cd-only"])
    assert not cd["fig"].exists()


def test_cd_only_sin_resumen_sellado_no_dibuja(cd: dict) -> None:
    with pytest.raises(SystemExit, match="significance_summary"):
        st.main(["--cd-only"])
    assert not cd["fig"].exists()


# ═══════════════════════════════ B2 · figura de hold-out sin reescribir la tabla
def test_report_figure_only_no_escribe_la_tabla(tmp_path: Path, monkeypatch) -> None:
    llamadas: list[tuple] = []
    tex = tmp_path / "results_table.tex"
    monkeypatch.setattr(report, "TEX", tex)

    def dibujar(*a: str) -> Path:
        llamadas.append(a)
        return tmp_path / "x.png"

    monkeypatch.setattr(report, "plot_winner_holdout", dibujar)
    report.main(["--figure-only", "ets"])
    assert llamadas == [("mexico", "F3", "FAD", "ets")] and not tex.exists()
