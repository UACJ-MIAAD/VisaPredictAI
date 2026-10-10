"""M74-F · deuda 6 — el registro de exclusiones y la sonda BLAS, probados SIN ajustar modelos.

La sonda se ejecuta sólo con orden del autor (es cómputo). Aquí se fija lo que no cuesta nada:
el universo que va a medir, el entorno de hilos que da a cada subproceso, el coste que anuncia,
dónde puede escribir y cómo clasifica un resultado. `ensayo` se sustituye por un doble.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "experiments"))

import probe_linalg_determinism as sonda  # noqa: E402

A6, C4 = "rederiv_a6df0f4_20260917T055408", "rederiv_953cd00_20261006T073927"


def test_el_registro_guarda_las_16_exclusiones_exactas_de_la_cuarta_campana() -> None:
    datos = json.loads(sonda.REGISTRY.read_text(encoding="utf-8"))
    c4 = datos["campanas"][C4]
    assert len(c4["FAD"]) == 6 and len(c4["DFF"]) == 10
    assert len(datos["campanas"][A6]["FAD"]) + len(datos["campanas"][A6]["DFF"]) == 14


def test_la_sonda_mide_la_union_y_sabe_quien_excluyo_cada_combinacion() -> None:
    combos = sonda.cargar_combos()
    assert len(combos) == 20
    por_clave = {(c.table, c.country, c.category, c.model): c.campaigns for c in combos}
    assert set(por_clave[("FAD", "india", "F2B", "sarima")]) == {A6, C4}  # excluida en ambas
    assert por_clave[("FAD", "india", "F1", "arima")] == (C4,)  # sólo en la cuarta
    assert por_clave[("DFF", "philippines", "F1", "sarima")] == (A6,)  # sólo en la anterior
    assert sum(len(c.campaigns) == 2 for c in combos) == 10, "16 + 14 − 20 = 10 en ambas"
    assert {c.model for c in combos} == {"arima", "sarima"}


def test_un_hilo_fija_todos_los_blas_y_default_no_fija_ninguno() -> None:
    base = {"VECLIB_MAXIMUM_THREADS": "8", "OMP_NUM_THREADS": "4", "PATH": "/x"}
    uno = sonda.entorno("1", base=base)
    assert all(uno[v] == "1" for v in sonda.THREAD_VARS) and uno["PATH"] == "/x"
    defecto = sonda.entorno("default", base=base)
    assert not any(v in defecto for v in sonda.THREAD_VARS), "heredar los hilos del padre falsearía la medida"
    with pytest.raises(ValueError):
        sonda.entorno("4", base=base)


def test_el_coste_anunciado_es_el_de_la_formula() -> None:
    combos = sonda.cargar_combos()
    n_s = sum(c.model == "sarima" for c in combos)
    n_a = len(combos) - n_s
    esperado = (n_s * (16.1 + 6.0) + n_a * (5.9 + 6.0)) * 5 * 2
    assert sonda.coste_estimado(combos, 5) == pytest.approx(esperado)
    assert 45 * 60 < esperado < 90 * 60


def test_dry_run_no_ajusta_nada(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sonda, "ensayo", lambda *_a: pytest.fail("--dry-run lanzó un ajuste"))
    assert sonda.main(["--dry-run"]) == 0
    assert "200 ajustes" in capsys.readouterr().out


def test_sin_out_o_con_out_dentro_de_reports_no_corre(monkeypatch) -> None:
    monkeypatch.setattr(sonda, "ensayo", lambda *_a: pytest.fail("corrió sin destino válido"))
    with pytest.raises(SystemExit, match="obligatorio"):
        sonda.main(["--repeats", "1"])
    with pytest.raises(SystemExit, match="dentro de reports"):
        sonda.main(["--repeats", "1", "--out", str(RAIZ / "reports" / "eval" / "probe.json")])


def test_clasifica_determinismo_por_combinacion_e_hilos(tmp_path: Path, monkeypatch) -> None:
    """Doble con la forma exacta del hijo: con 1 hilo estable, con hilos por defecto alterna."""
    llamadas: dict[str, int] = {}

    def doble(combo, config):
        k = f"{combo.table}{combo.country}{combo.category}{combo.model}{config}"
        llamadas[k] = llamadas.get(k, 0) + 1
        if config == "default" and llamadas[k] % 2:
            return {"status": "LinAlgError", "forecast_sha256": None, "n": 0, "secs": 0.1}
        return {"status": "ok", "forecast_sha256": "h" + config, "n": 24, "secs": 0.1}

    monkeypatch.setattr(sonda, "ensayo", doble)
    out = tmp_path / "probe.json"
    assert sonda.main(["--repeats", "2", "--out", str(out)]) == 0
    informe = json.loads(out.read_text())
    assert informe["schema"] == "linalg-probe/1" and len(informe["trials"]) == 80
    por_hilos = {(s["threads"]): s for s in informe["summary"] if s["country"] == "india" and s["category"] == "F1"}
    assert por_hilos["1"]["deterministic"] is True and por_hilos["1"]["n_fail"] == 0
    assert por_hilos["default"]["deterministic"] is False and por_hilos["default"]["n_fail"] == 1
