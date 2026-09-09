"""E2 · El barrido por cohorte no puede inventarse el universo ni el contraste.

Un barrido exploratorio es fácil de falsear sin querer: basta compararse contra un piso global
en vez del de la propia cohorte, mezclar tablas con protocolos distintos, agrupar mal la familia
de Holm, deslizar un contraste unilateral o dejar pasar una serie duplicada. Cada una de esas
formas de romperlo tiene aquí una prueba que la pone roja, y el contraste exacto se valida
contra la enumeración explícita de los patrones de signo.
"""

from __future__ import annotations

import itertools
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import experiments.scan_cohorts as sc
from vp_model import exact_tests

ROOT = Path(__file__).resolve().parent.parent
ARTEFACTO = ROOT / "reports" / "eval" / "cohort_scan.json"
COHORTES = ROOT / "reports" / "eval" / "series_cohorts.json"


# ------------------------------------------------------------------ contraste exacto
@pytest.mark.parametrize("n", [2, 3, 4, 5, 6, 7, 8])
def test_la_p_exacta_coincide_con_la_enumeracion_explicita(n: int) -> None:
    """La nula se enumera a mano sobre los 2^n patrones de signo."""
    rng = np.random.default_rng(100 + n)
    for _ in range(15):
        d = np.round(rng.normal(0.0, 1.0, n), 3)
        if np.any(d == 0.0):
            continue
        r = exact_tests.signed_rank_exact(d)
        rangos = exact_tests.midranks(np.abs(d))
        centro = rangos.sum() / 2.0
        obs = abs(rangos[d > 0].sum() - centro)
        masa = sum(
            1
            for signos in itertools.product([1, -1], repeat=n)
            if abs(rangos[np.array(signos) > 0].sum() - centro) >= obs - 1e-12
        )
        assert r.p_value == pytest.approx(masa / 2**n, abs=1e-12)


def test_la_nula_condicional_sigue_siendo_exacta_con_empates() -> None:
    """Con empates la tabla clásica de rangos deja de valer; la condicional no."""
    d = np.array([1.0, -1.0, 1.0, 2.0, -2.0, 3.0, 1.0])
    r = exact_tests.signed_rank_exact(d)
    assert r.ties_present
    rangos = exact_tests.midranks(np.abs(d))
    centro = rangos.sum() / 2.0
    obs = abs(rangos[d > 0].sum() - centro)
    masa = sum(
        1
        for s in itertools.product([1, -1], repeat=len(d))
        if abs(rangos[np.array(s) > 0].sum() - centro) >= obs - 1e-12
    )
    assert r.p_value == pytest.approx(masa / 2 ** len(d), abs=1e-12)


def test_el_contraste_es_BILATERAL() -> None:
    """Un contraste unilateral daría la mitad: espejar el signo no puede cambiar la p."""
    d = np.array([0.4, 0.9, 0.2, 1.1, 0.7, 0.3, 0.8])
    arriba = exact_tests.signed_rank_exact(d)
    abajo = exact_tests.signed_rank_exact(-d)
    assert arriba.p_value == pytest.approx(abajo.p_value, abs=1e-15)
    assert arriba.hodges_lehmann == pytest.approx(-abajo.hodges_lehmann)
    n = len(d)
    assert arriba.p_value == pytest.approx(2 / 2**n, abs=1e-12)  # bilateral, no 1/2^n


def test_los_ceros_se_descartan_y_n_eff_lo_dice() -> None:
    d = np.array([0.0, 0.5, 0.0, -0.3, 0.7, 0.9, 0.1, 0.2])
    r = exact_tests.signed_rank_exact(d)
    assert (r.n_pairs, r.n_eff, r.n_zeros) == (8, 6, 2)
    assert r.p_value == exact_tests.signed_rank_exact(d[d != 0]).p_value


def test_todo_ceros_no_es_un_contraste() -> None:
    with pytest.raises(ValueError, match="todas las diferencias son cero"):
        exact_tests.signed_rank_exact(np.zeros(8))


def test_holm_es_monotono_y_escala_con_la_familia() -> None:
    p = {"a": 0.001, "b": 0.02, "c": 0.04}
    aj = exact_tests.holm_adjust(p)
    assert aj["a"] == pytest.approx(0.003) and aj["b"] == pytest.approx(0.04)
    assert aj["c"] >= aj["b"] >= aj["a"]
    # Una familia MÁS grande no puede dar una p ajustada menor.
    grande = exact_tests.holm_adjust({**p, "d": 0.5, "e": 0.6})
    assert all(grande[k] >= aj[k] for k in p)


# ------------------------------------------------------------- universo y fail-closed
def _escribir(marco: pd.DataFrame, tmp: Path) -> None:
    """Reparte el material entre los cuatro cuadernos SIN duplicar ninguna fila.

    Hay dos cuadernos por tabla (familiar y empleo); escribir el mismo marco en los dos haría
    que el cargador viera cada serie dos veces —y con razón: es exactamente el duplicado que el
    barrido debe rechazar—.
    """
    for nombre in sc.SCORECARDS:
        tabla = "FAD" if "FAD" in nombre else "DFF"
        propio = marco[marco.table == tabla] if "EB_" not in nombre else marco.iloc[0:0]
        propio.to_csv(tmp / nombre, index=False)


def _material(tmp: Path, *, series: list[tuple[str, str, str, str]], modelos=("naive1", "m1", "m2")):
    """Escribe un catálogo de cohortes y un cuaderno de puntuación coherentes."""
    rng = np.random.default_rng(7)
    filas = []
    for modelo in modelos:
        for pais, cat, tabla, _coh in series:
            base = 0.08 + (0.0 if modelo == "naive1" else 0.02)
            filas.append(
                {
                    "model": modelo,
                    "country": pais,
                    "category": cat,
                    "table": tabla,
                    "hold_mase": float(base + rng.normal(0, 0.005)),
                }
            )
    marco = pd.DataFrame(filas)
    _escribir(marco, tmp)
    cohortes = {
        "rule_version": "1.0.0",
        "population": {"n_evaluable": len(series)},
        "series": [{"country": p, "category": c, "table": t, "cohort": co} for p, c, t, co in series],
    }
    ruta = tmp / "series_cohorts.json"
    ruta.write_text(json.dumps(cohortes, indent=2, sort_keys=True))
    return ruta, marco


@pytest.fixture
def material(tmp_path: Path):
    series = [(f"p{i}", "F1", "FAD", "estable" if i % 2 else "no_estable") for i in range(16)]
    series += [(f"q{i}", "F2", "DFF", "estable") for i in range(8)]
    return _material(tmp_path, series=series), tmp_path


def test_el_barrido_corre_sobre_material_sintetico(material) -> None:
    (cohortes, _), tmp = material
    informe = sc.scan(base=tmp, cohorts_path=cohortes)
    assert informe["status"] == "exploratorio"
    assert informe["population"]["n_evaluable"] == 24
    assert {(c["table"], c["cohort"]) for c in informe["cells"]} == {
        ("FAD", "estable"),
        ("FAD", "no_estable"),
        ("DFF", "estable"),
    }


def test_una_serie_duplicada_detiene_el_barrido(material) -> None:
    (cohortes, marco), tmp = material
    doble = pd.concat([marco, marco.head(1)], ignore_index=True)
    _escribir(doble, tmp)
    with pytest.raises(sc.UniversoIncoherente, match="duplicadas"):
        sc.scan(base=tmp, cohorts_path=cohortes)


def test_una_serie_ausente_detiene_el_barrido(material) -> None:
    (cohortes, marco), tmp = material
    recortado = marco[~((marco.model == "naive1") & (marco.country == "p0"))]
    _escribir(recortado, tmp)
    with pytest.raises(sc.UniversoIncoherente, match="no tienen naive1 puntuado"):
        sc.scan(base=tmp, cohorts_path=cohortes)


def test_un_catalogo_que_no_cuadra_con_su_poblacion_detiene_el_barrido(material) -> None:
    (cohortes, _), tmp = material
    d = json.loads(cohortes.read_text())
    d["population"]["n_evaluable"] = 999
    cohortes.write_text(json.dumps(d))
    with pytest.raises(sc.UniversoIncoherente, match="declara 999"):
        sc.scan(base=tmp, cohorts_path=cohortes)


def test_falta_un_cuaderno_y_el_barrido_para(material) -> None:
    (cohortes, _), tmp = material
    (tmp / "model_comparison_FAD21.csv").unlink()
    with pytest.raises(sc.UniversoIncoherente, match="falta el cuaderno"):
        sc.scan(base=tmp, cohorts_path=cohortes)


# --------------------------------------------------- el piso es de la celda, no global
def test_cada_celda_se_compara_contra_SU_naive1(material) -> None:
    """RED del piso global: mover el naive1 de UNA celda no puede tocar las otras."""
    (cohortes, marco), tmp = material
    antes = sc.scan(base=tmp, cohorts_path=cohortes)
    peor = marco.copy()
    objetivo = (peor.model == "naive1") & (peor.table == "FAD") & (peor.category == "F1")
    peor.loc[objetivo, "hold_mase"] = peor.loc[objetivo, "hold_mase"] * 50.0
    _escribir(peor, tmp)
    despues = sc.scan(base=tmp, cohorts_path=cohortes)
    dff_antes = [c for c in antes["cells"] if c["table"] == "DFF"][0]
    dff_despues = [c for c in despues["cells"] if c["table"] == "DFF"][0]
    assert dff_antes == dff_despues, "el naive1 de FAD contaminó la celda DFF: piso global"
    fad_antes = [c for c in antes["cells"] if c["table"] == "FAD"][0]
    fad_despues = [c for c in despues["cells"] if c["table"] == "FAD"][0]
    assert fad_antes != fad_despues, "mover el propio piso de la celda debería cambiarla"


def test_las_tablas_no_se_mezclan(material) -> None:
    """Protocolo mezclado: FAD y DFF tienen MIN_TRAIN distinto y son celdas distintas."""
    (cohortes, _), tmp = material
    informe = sc.scan(base=tmp, cohorts_path=cohortes)
    for celda in informe["cells"]:
        assert {s["table"] for s in celda["series"]} == {celda["table"]}
        assert celda["min_train"] == sc.MIN_TRAIN[celda["table"]]


def test_las_cohortes_no_se_mezclan(material) -> None:
    (cohortes, _), tmp = material
    catalogo = {
        (s["country"], s["category"], s["table"]): s["cohort"] for s in json.loads(cohortes.read_text())["series"]
    }
    for celda in sc.scan(base=tmp, cohorts_path=cohortes)["cells"]:
        for s in celda["series"]:
            assert catalogo[(s["country"], s["category"], s["table"])] == celda["cohort"]


def test_cambiar_la_cohorte_de_una_serie_cambia_su_celda(material) -> None:
    """RED de cohorte incorrecta: la partición gobierna de verdad el agrupamiento."""
    (cohortes, _), tmp = material
    antes = {(c["table"], c["cohort"]): c["n_series"] for c in sc.scan(base=tmp, cohorts_path=cohortes)["cells"]}
    d = json.loads(cohortes.read_text())
    for s in d["series"]:
        if s["country"] == "p1":
            s["cohort"] = "no_estable"
    cohortes.write_text(json.dumps(d))
    despues = {(c["table"], c["cohort"]): c["n_series"] for c in sc.scan(base=tmp, cohorts_path=cohortes)["cells"]}
    assert despues[("FAD", "estable")] == antes[("FAD", "estable")] - 1
    assert despues[("FAD", "no_estable")] == antes[("FAD", "no_estable")] + 1


# ------------------------------------------------------------------------- Holm
def test_la_familia_de_holm_es_la_celda(material) -> None:
    (cohortes, _), tmp = material
    for celda in sc.scan(base=tmp, cohorts_path=cohortes)["cells"]:
        contrastados = [m for m in celda["models"] if "p_holm" in m]
        assert celda["holm_family_size"] == len(contrastados)
        # Recalculado aquí: la familia es exactamente esta celda, ni una hipótesis más ni menos.
        esperado = exact_tests.holm_adjust({m["model"]: m["p_value"] for m in contrastados})
        for m in contrastados:
            assert m["p_holm"] == pytest.approx(esperado[m["model"]], abs=1e-15), m["model"]
        if len(contrastados) > 1:
            assert any(m["p_holm"] > m["p_value"] for m in contrastados), (
                "con más de una hipótesis, Holm tiene que corregir algo"
            )


def test_holm_mal_agrupado_daria_otro_resultado(material) -> None:
    """Agrupar TODAS las celdas en una familia infla las p ajustadas: no es lo que hacemos."""
    (cohortes, _), tmp = material
    informe = sc.scan(base=tmp, cohorts_path=cohortes)
    por_celda = {
        (c["table"], c["cohort"], m["model"]): m["p_holm"]
        for c in informe["cells"]
        for m in c["models"]
        if "p_holm" in m
    }
    global_ = exact_tests.holm_adjust(
        {
            f"{c['table']}|{c['cohort']}|{m['model']}": m["p_value"]
            for c in informe["cells"]
            for m in c["models"]
            if "p_value" in m
        }
    )
    assert por_celda, "sin contrastes no hay nada que comprobar"
    assert any(global_[f"{t}|{co}|{mo}"] > p for (t, co, mo), p in por_celda.items()), (
        "la familia por celda debe ser MENOS conservadora que una familia global"
    )


# ------------------------------------------------------- determinismo y procedencia
def test_determinismo_entre_procesos(material, tmp_path: Path) -> None:
    (cohortes, _), tmp = material
    salidas = []
    for i in range(2):
        destino = tmp_path / f"salida{i}.json"
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys,json,pathlib;sys.path.insert(0,sys.argv[1]);"
                "import experiments.scan_cohorts as sc;"
                "inf=sc.scan(base=pathlib.Path(sys.argv[2]),cohorts_path=pathlib.Path(sys.argv[3]));"
                "sc.write(inf,pathlib.Path(sys.argv[4]))",
                str(ROOT),
                str(tmp),
                str(cohortes),
                str(destino),
            ],
            check=True,
            capture_output=True,
            cwd=ROOT,
            env={"PYTHONHASHSEED": str(i), **__import__("os").environ},
        )
        salidas.append(destino.read_bytes())
    assert salidas[0] == salidas[1]


def test_la_procedencia_registra_los_hashes_de_entrada(material) -> None:
    (cohortes, _), tmp = material
    informe = sc.scan(base=tmp, cohorts_path=cohortes)
    prov = informe["provenance"]
    assert set(prov["scorecards"]) == set(sc.SCORECARDS)
    assert all(len(h) == 64 for h in prov["scorecards"].values())
    assert len(prov["cohorts_sha256"]) == 64


def test_manipular_una_entrada_despues_cambia_la_procedencia(material) -> None:
    """RED de manipulación posterior: los hashes deben moverse con el contenido."""
    (cohortes, marco), tmp = material
    antes = sc.scan(base=tmp, cohorts_path=cohortes)["provenance"]
    tocado = marco.copy()
    tocado.loc[tocado.index[0], "hold_mase"] = 9.9
    _escribir(tocado, tmp)
    despues = sc.scan(base=tmp, cohorts_path=cohortes)["provenance"]
    assert antes["scorecards"] != despues["scorecards"]
    assert antes["cohorts_sha256"] == despues["cohorts_sha256"]


def test_el_mcs_es_complementario_y_no_decide_nada(material) -> None:
    (cohortes, _), tmp = material
    for celda in sc.scan(base=tmp, cohorts_path=cohortes)["cells"]:
        if celda["mcs90"] is None:
            assert celda["n_series"] < exact_tests.MIN_PAIRS
            continue
        assert celda["mcs90"]["level"] == 0.90
        assert "complementario" in celda["mcs90"]["note"]
        assert celda["mcs90"]["seed"] == sc.MCS_SEED


# -------------------------------------------------------------------- artefacto vivo
@pytest.mark.skipif(not ARTEFACTO.exists(), reason="el barrido aún no se ha generado")
class TestArtefactoPublicado:
    def test_esta_al_dia(self, tmp_path: Path) -> None:
        """Maestro dorado: el archivo del repo es lo que producen las entradas de hoy."""
        fresco = sc.write(sc.scan(), tmp_path / "fresco.json")
        assert fresco.read_bytes() == ARTEFACTO.read_bytes(), (
            "cohort_scan.json está rancio: corre `python experiments/scan_cohorts.py`"
        )

    def test_reproduce_el_universo_publicado_por_E1(self) -> None:
        cohortes = json.loads(COHORTES.read_text())
        informe = json.loads(ARTEFACTO.read_text())
        assert informe["population"]["n_evaluable"] == cohortes["population"]["n_evaluable"]
        assert informe["rule_version"] == cohortes["rule_version"]
        vistas = [tuple(s.values()) for c in informe["cells"] for s in c["series"]]
        assert len(vistas) == len(set(vistas)) == cohortes["population"]["n_evaluable"]

    def test_todo_queda_etiquetado_exploratorio(self) -> None:
        informe = json.loads(ARTEFACTO.read_text())
        assert informe["status"] == "exploratorio"
        vistos = {m["verdict_exploratory"] for c in informe["cells"] for m in c["models"]}
        assert vistos <= {"bate_naive1", "peor_que_naive1", "sin_evidencia", "sin_datos_suficientes"}
        for c in informe["cells"]:
            for m in c["models"]:
                assert "verdict_exploratory" in m

    def test_ninguna_p_exacta_se_publica_como_cero(self) -> None:
        """Una p exacta nunca vale 0: con n_eff pares, el mínimo es 2/2^n_eff."""
        informe = json.loads(ARTEFACTO.read_text())
        for c in informe["cells"]:
            for m in c["models"]:
                if "p_value" in m:
                    assert m["p_value"] >= 2 / 2 ** m["n_eff"] - 1e-18, (c["table"], m["model"])
                    assert m["p_value"] > 0.0
