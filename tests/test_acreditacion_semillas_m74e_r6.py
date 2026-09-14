"""M74-E-R6 · el agregador multi-semilla acredita el grupo EXACTO antes de leerlo (B1).

`eval_global_deep` lee por glob `global_<tabla>_*.csv`; `aggregate_seeds` exigía el conjunto de
variantes s1..s5 pero no de QUÉ corrida eran. Y la acreditación no podía existir: el productor de
sidecars (`seed_coverage.coverage_sidecar`) estaba escrito y **ningún productor lo llamaba**. Estas
pruebas recorren productor → sidecar → lector con las funciones REALES de los dos lados.

★ M74-E-R7 cambia el andamiaje, no la intención: el grupo se escribe con el productor sobre la
rejilla canónica de un panel sintético, porque el lector ya no acepta marcos de 3 filas.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pandas as pd
import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "experiments"))
sys.path.insert(0, str(RAIZ))

import aggregate_seeds as ag  # noqa: E402
import run_global_deep as rgd  # noqa: E402

SHA = "b" * 40


def _grupo(camp: Path, nivel: pd.DataFrame, cid: str, variante: str = "camp_diff", semillas=(1, 2, 3, 4, 5)) -> None:
    import seed_coverage

    from tools.check_campaign_completeness import SEED_MODELS

    rejilla = seed_coverage.canonical_grid(nivel, 24)
    for k, m in enumerate(SEED_MODELS[variante]):
        rejilla[m] = rejilla["y"] + 1.0 + k
    merged = nivel.merge(rejilla.drop(columns=["y"]), on=["unique_id", "ds"], how="left")
    camp.mkdir(parents=True, exist_ok=True)
    for i in semillas:
        out = camp / f"global_FAD_{variante}_s{i}.csv"
        semilla = rgd._SEMILLA_RE.match(f"{variante}_s{i}")
        rgd._sellar_semilla(merged, nivel, out, "FAD", semilla, campaign={"campaign_id": cid, "source_git_sha": SHA})


def test_control_el_grupo_de_esta_campana_se_acredita(tmp_path: Path, panel_semillas) -> None:
    _grupo(tmp_path / "c", panel_semillas, "camp_actual")
    ag.acreditar_grupo("FAD", "camp_diff_s", tmp_path / "c", campaign_id="camp_actual", source_git_sha=SHA)


def test_RED_archivos_viejos_con_el_conteo_correcto_no_se_agregan(tmp_path: Path, panel_semillas) -> None:
    """★ El ataque de la auditoría: cinco semillas completas, pero de OTRA corrida."""
    _grupo(tmp_path / "c", panel_semillas, "camp_vieja")
    with pytest.raises(SystemExit, match="campaign_id"):
        ag.acreditar_grupo("FAD", "camp_diff_s", tmp_path / "c", campaign_id="camp_actual", source_git_sha=SHA)


def test_RED_un_csv_tocado_despues_de_sellarse(tmp_path: Path, panel_semillas) -> None:
    """Un pronóstico cambiado a mano deja la rejilla intacta: lo caza el hash de los bytes."""
    _grupo(tmp_path / "c", panel_semillas, "camp_actual")
    csv = tmp_path / "c" / "global_FAD_camp_diff_s3.csv"
    marco = pd.read_csv(csv)
    marco.loc[0, "BiTCN"] += 1.0
    marco.to_csv(csv, index=False)
    with pytest.raises(SystemExit, match="csv_sha256"):
        ag.acreditar_grupo("FAD", "camp_diff_s", tmp_path / "c", campaign_id="camp_actual", source_git_sha=SHA)


def test_RED_una_semilla_sin_sidecar(tmp_path: Path, panel_semillas) -> None:
    _grupo(tmp_path / "c", panel_semillas, "camp_actual")
    (tmp_path / "c" / "coverage_FAD_camp_diff_s2.json").unlink()
    with pytest.raises(SystemExit, match="sin sidecar"):
        ag.acreditar_grupo("FAD", "camp_diff_s", tmp_path / "c", campaign_id="camp_actual", source_git_sha=SHA)


def test_RED_una_semilla_de_mas(tmp_path: Path, panel_semillas) -> None:
    _grupo(tmp_path / "c", panel_semillas, "camp_actual", semillas=(1, 2, 3, 4, 5, 6))
    with pytest.raises(SystemExit, match="grupo"):
        ag.acreditar_grupo("FAD", "camp_diff_s", tmp_path / "c", campaign_id="camp_actual", source_git_sha=SHA)


def test_RED_otro_sha_de_codigo(tmp_path: Path, panel_semillas) -> None:
    _grupo(tmp_path / "c", panel_semillas, "camp_actual")
    with pytest.raises(SystemExit, match="source_git_sha"):
        ag.acreditar_grupo("FAD", "camp_diff_s", tmp_path / "c", campaign_id="camp_actual", source_git_sha="c" * 40)


def test_fuera_de_las_semillas_de_campana_no_se_sella() -> None:
    """La búsqueda HPO y las corridas sueltas no son un grupo de semillas: no pasan por el sellado."""
    for sufijo in ("camp_hposearch", "levels", "diff", "camp_diff"):
        assert rgd._SEMILLA_RE.match(sufijo) is None
    assert rgd._SEMILLA_RE.match("camp_diff_s3") is not None


def test_el_agregador_acredita_ANTES_de_leer() -> None:
    """Por AST: la llamada a `acreditar_grupo` precede a `eval_global_deep` dentro de `main`."""
    arbol = ast.parse((RAIZ / "experiments" / "aggregate_seeds.py").read_text(encoding="utf-8"))
    main = next(n for n in arbol.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    orden = [
        n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
        for n in ast.walk(main)
        if isinstance(n, ast.Call)
    ]
    assert "acreditar_grupo" in orden and "eval_global_deep" in orden
    lineas = {
        (n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")): n.lineno
        for n in ast.walk(main)
        if isinstance(n, ast.Call)
    }
    assert lineas["acreditar_grupo"] < lineas["eval_global_deep"]
