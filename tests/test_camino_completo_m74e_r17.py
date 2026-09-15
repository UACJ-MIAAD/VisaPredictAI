"""M74-E-R17 · lo que la cuarta auditoría ciega (sobre `6edefa4`) encontró ejecutando el camino REAL.

Tres etapas obligatorias fallaban de forma determinista por código, invisibles para el smoke (que sólo
importa) y para la batería (fixtures que no reproducían el disco real):

1. `build_model_card.py` devuelve 1 SIEMPRE desde D4 (la tarjeta la emite el manifiesto de release) y el
   runbook lo invocaba con `run_req` en la etapa 8 ⇒ `failed` tras ≥12 h.
2. `run_e3_campaign.py` pasaba `--recipe` a `run_global_gbm.py`, que no lo declaraba (exit 2 en los seis
   lanes GBM), y no pasaba `--receipt` a ningún lane, así que `score_e3_campaign` puntuaba recibos de la
   campaña anterior sobre CSV nuevos.
3. El gate de completitud contaba `hpo_deep_best_*_Auto*.receipt.json` como ganadoras (6 ≠ 3) ⇒ exit 4.

Y dos límites del mismo informe: el archivado de la transacción anterior iba después de escribir el
manifiesto nuevo; y `tree_dirty()` del runbook sólo miraba código mientras el preflight sella el
`porcelain` entero, con un exit 13 engañoso para quien no había commiteado las salidas previas.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "experiments"))

from tools import check_campaign_completeness as gate  # noqa: E402
from tools.check_dvc_lock_fresh import resolve_dvc  # noqa: E402

RUNBOOK = RAIZ / "experiments" / "run_rederivation.sh"
REAL_DVC = resolve_dvc(RAIZ, os.environ)


def _vivas(p: Path) -> str:
    return "\n".join(ln for ln in p.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#"))


# ═══════════════════════════════ 1 · la etapa 8 ya no invoca un productor que siempre falla
def test_RED_el_runbook_no_invoca_build_model_card() -> None:
    assert "build_model_card" not in _vivas(RUNBOOK)


def test_build_model_card_sigue_siendo_un_stub_de_D4_y_por_eso_no_puede_ser_run_req() -> None:
    """Documenta la razón: si algún día vuelve a producir, esta prueba avisa y la etapa puede reconsiderarse."""
    fin = subprocess.run(
        [sys.executable, str(RAIZ / "experiments" / "build_model_card.py")],
        cwd=RAIZ,
        capture_output=True,
        text=True,
        timeout=120,
        env={"PATH": "/usr/bin:/bin", "HOME": str(RAIZ), "PYTHONPATH": str(RAIZ)},
    )
    assert fin.returncode == 1 and "release-manifest" in fin.stderr


# ═══════════════════════════════ 2 · cada lane de E3 se acepta por su runner y deja recibo
@pytest.mark.skipif(find_spec("lightgbm") is None or find_spec("darts") is None, reason="runners del extra model")
def test_RED_las_ordenes_reales_de_los_42_lanes_las_acepta_su_runner() -> None:
    import run_e3_campaign as e3
    import run_global_deep as deep
    import run_global_gbm as gbm

    lanes = e3.plan()
    assert len(lanes) == 42
    parsers = {"run_global_deep.py": deep.build_parser, "run_global_gbm.py": gbm.build_parser}
    for lane in lanes:
        args = parsers[lane.runner]().parse_args(lane.command[2:])  # SystemExit 2 contra 6edefa4 en los 6 GBM
        assert args.recipe == lane.recipe and args.cohort == lane.cohort and args.table == lane.table
        assert args.receipt == e3.RECEIPTS / f"receipt_{lane.recipe}_{lane.table}_{lane.cohort}.json"
        if lane.runner == "run_global_gbm.py":
            gbm._receta_gbm(args)
            assert args.models == ["lightgbm"]


@pytest.mark.skipif(find_spec("lightgbm") is None or find_spec("darts") is None, reason="runner GBM del extra model")
def test_una_receta_que_no_declara_al_gbm_como_runner_no_corre_en_el_gbm() -> None:
    import run_global_gbm as gbm

    args = gbm.build_parser().parse_args(["--recipe", "control-bitcn", "--table", "FAD"])
    with pytest.raises(SystemExit, match="runner"):
        gbm._receta_gbm(args)


def test_los_recibos_de_los_lanes_van_donde_los_lee_el_puntuador() -> None:
    import run_e3_campaign as e3
    import score_e3_campaign as score

    assert (RAIZ / e3.RECEIPTS).resolve() == score.LANES.resolve()


# ═══════════════════════════════ 3 · el gate no confunde recibos con ganadoras, y los exige
HPO = {
    "AutoBiTCN": {"learning_rate": 1e-3, "max_steps": 1, "input_size": 1, "scaler_type": "s", "hidden_size": 1, "dropout": 0.1},
    "AutoNHITS": {"learning_rate": 1e-3, "max_steps": 1, "input_size": 1, "scaler_type": "s", "n_pool_kernel_size": [1], "n_freq_downsample": [1]},
    "AutoTiDE": {"learning_rate": 1e-3, "max_steps": 1, "input_size": 1, "scaler_type": "s", "hidden_size": 1, "decoder_output_dim": 1},
}  # fmt: skip


def _ganadoras(root: Path, *, con_recibo: bool) -> None:
    import json

    camp = root / "reports" / "campaign"
    camp.mkdir(parents=True, exist_ok=True)
    for m, cfg in HPO.items():
        (camp / f"hpo_deep_best_FAD_{m}.json").write_text(json.dumps(cfg), encoding="utf-8")
        if con_recibo:
            (camp / f"hpo_deep_best_FAD_{m}.receipt.json").write_text(
                '{"schema": "hpo-winner-receipt/1"}', encoding="utf-8"
            )


def test_RED_los_recibos_del_hpo_no_cuentan_como_ganadoras(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Contra `6edefa4`: `CONTEO … esperados 3, hallados 6` y el runbook salía 4 en la etapa 6.5."""
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    _ganadoras(tmp_path, con_recibo=True)
    assert gate._check_list([("reports/campaign/hpo_deep_best_FAD_Auto*.json", 3, 0)], None, True) == []


def test_RED_una_ganadora_sin_recibo_no_pasa_el_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    _ganadoras(tmp_path, con_recibo=False)
    problemas = gate._check_list([("reports/campaign/hpo_deep_best_FAD_Auto*.json", 3, 0)], None, True)
    assert len(problemas) == 3 and all("sin recibo" in p for p in problemas)


# ═══════════════════════════════ 4 · relanzamiento: archivar antes del manifiesto, y salidas sin commitear
def test_RED_una_campana_terminada_se_archiva_antes_de_escribir_el_manifiesto() -> None:
    vivo = _vivas(RUNBOOK)
    assert vivo.index("archive --dir") < vivo.index("> reports/campaign/campaign_manifest.json")


def _tree_dirty_real() -> str:
    guion = RUNBOOK.read_text(encoding="utf-8")
    funcion = re.search(r"^tree_dirty\(\) \{\n.*?^\}\n", guion, flags=re.M | re.S)
    assert funcion
    return funcion.group(0)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copy(RAIZ / ".gitignore", repo / ".gitignore")
    (repo / "reports" / "eval").mkdir(parents=True)
    (repo / "reports" / "eval" / "base.csv").write_text("a\n", encoding="utf-8")
    git = ["git", "-c", "user.email=e@l", "-c", "user.name=e"]
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run([*git, "add", "-A"], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-qm", "base"], cwd=repo, check=True)
    return repo


def _tree_dirty_en(repo: Path) -> str:
    guion = repo.parent / "td.sh"  # fuera del repo: dentro seria un .sh untracked y la guarda lo acusaria
    guion.write_text("#!/bin/bash\n" + _tree_dirty_real() + "tree_dirty\n", encoding="utf-8")
    return subprocess.run(["bash", str(guion)], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def test_RED_tree_dirty_nombra_las_salidas_sin_commitear_que_el_preflight_veria(tmp_path: Path) -> None:
    """Contra `6edefa4` devolvía vacío y la campaña moría después en exit 13 con «sello de otra identidad»."""
    repo = _repo(tmp_path)
    assert _tree_dirty_en(repo) == ""
    (repo / "reports" / "eval" / "holdout_forecasts_FAD.csv.receipt.json").write_text("{}\n", encoding="utf-8")
    assert _tree_dirty_en(repo) == "salidas-sin-commitear"


def test_tree_dirty_sigue_limpio_con_el_estado_ignorado_de_la_transaccion(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    for rel in ("reports/campaign/campaign.json", "reports/campaign/campaign.json.lock", "reports/logs/x.log"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("{}\n", encoding="utf-8")
    assert _tree_dirty_en(repo) == ""


def test_tree_dirty_sigue_prefiriendo_el_diagnostico_de_codigo(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "suelto.py").write_text("x = 1\n", encoding="utf-8")
    assert _tree_dirty_en(repo) == "codigo-untracked"


# ═══════════════════════════════ 5 · el staging huérfano tampoco entra al puntero
@pytest.mark.skipif(REAL_DVC is None, reason="DVC gobernado (ante/bin/dvc o $VP_DVC) ausente")
def test_RED_dvc_add_models_excluye_el_staging(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "models" / ".staging" / "rederiv_x").mkdir(parents=True)
    (repo / "models" / "a.txt").write_text("a", encoding="utf-8")
    (repo / "models" / ".staging" / "rederiv_x" / "b.txt").write_text("b", encoding="utf-8")
    shutil.copy(RAIZ / ".dvcignore", repo / ".dvcignore")
    env = {**os.environ, "DVC_NO_ANALYTICS": "1"}
    subprocess.run([str(REAL_DVC), "init", "--no-scm", "-q"], cwd=repo, check=True, capture_output=True, env=env)
    subprocess.run([str(REAL_DVC), "add", "models", "-q"], cwd=repo, check=True, capture_output=True, env=env)
    assert re.search(r"nfiles:\s*1\b", (repo / "models.dvc").read_text(encoding="utf-8"))
