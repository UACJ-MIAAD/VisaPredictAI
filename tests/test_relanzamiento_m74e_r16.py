"""M74-E-R16 · el estado de la transacción no puede ensuciar el sello del relanzamiento.

La tercera auditoría ciega (sobre `676169f`, dictamen APTO para el primer arranque) ensayó lo que R9–R14 habían
dejado como «deducido, no ensayado» o «por diseño»: `reports/campaign/campaign.json`, su `.lock` (que
`campaign_state` crea en cada transición y nunca borra) y `reports/campaign/transactions/` no estaban ignorados
en git, el preflight mide `git status --porcelain` con untracked incluidos, y el manifiesto se escribe con
`dirty=false` ⇒ `--assert-sealed` rechazaba el sello («sello de otra identidad») y toda campaña oficial
posterior a la primera moría en exit 13 antes de archivar. Son estado local del runbook, como
`campaign_manifest.json` y `reports/logs/`, y desde R16 se ignoran igual.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
ESTADO = (
    "reports/campaign/campaign.json",
    "reports/campaign/campaign.json.lock",
    "reports/campaign/transactions/rederiv_aaaaaaa_20260915T000000.json",
)


def _repo_con_el_gitignore_real(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copy(RAIZ / ".gitignore", repo / ".gitignore")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=e@l", "-c", "user.name=e", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=e@l", "-c", "user.name=e", "commit", "-qm", "base"], cwd=repo, check=True)
    return repo


def _porcelain(repo: Path) -> str:
    return subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True).stdout


def test_RED_el_estado_de_la_transaccion_no_ensucia_el_arbol_que_el_preflight_mide(tmp_path: Path) -> None:
    """Contra `676169f`: los tres aparecen como `??` y `git.dirty` del sello pasa a true."""
    repo = _repo_con_el_gitignore_real(tmp_path)
    assert _porcelain(repo) == ""
    for rel in ESTADO:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("{}\n", encoding="utf-8")
    assert _porcelain(repo) == "", _porcelain(repo)


@pytest.mark.parametrize("rel", ESTADO)
def test_cada_ruta_de_estado_esta_ignorada_por_una_regla_explicita(tmp_path: Path, rel: str) -> None:
    repo = _repo_con_el_gitignore_real(tmp_path)
    fin = subprocess.run(["git", "check-ignore", "-v", rel], cwd=repo, capture_output=True, text=True, check=False)
    assert fin.returncode == 0 and ".gitignore:" in fin.stdout, fin.stdout


def test_lo_que_si_es_procedencia_versionada_sigue_sin_ignorarse(tmp_path: Path) -> None:
    """Los pools, los recibos de hold-out y los sidecars de semillas son procedencia: no se ignoran."""
    repo = _repo_con_el_gitignore_real(tmp_path)
    for rel in (
        "reports/campaign/campaign_pool_FAD_family.csv",
        "reports/eval/holdout_forecasts_FAD.csv.receipt.json",
        "reports/campaign/coverage_FAD_camp_diff_s1.json",
    ):
        fin = subprocess.run(["git", "check-ignore", "-q", rel], cwd=repo, capture_output=True, check=False)
        assert fin.returncode == 1, f"{rel} quedó ignorado"
