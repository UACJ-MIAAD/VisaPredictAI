"""M74-F · deuda 4 — deep-PI corre ANTES de conformal, y conformal sólo usa un deep-PI acreditado.

En la cuarta campaña real `improve_conformal` (etapa 4) derivó su baseline de `deep_pi_{tabla}.csv`
cuando ese CSV no se reescribía hasta la etapa 5: comparaba contra el deep-PI de la corrida anterior.
Se fija (1) el orden en las líneas VIVAS del runbook y (2) la acreditación al leer, con un repo git
temporal para que `campaign_identity` compare el HEAD de verdad. Nada toca `reports/` vivo.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "experiments"))

RUNBOOK = RAIZ / "experiments" / "run_rederivation.sh"


def _vivas(guion: Path) -> str:
    return "\n".join(ln for ln in guion.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#"))


def test_RED_deep_pi_corre_antes_que_conformal_en_el_runbook() -> None:
    """Contra `953cd00`: `improve_conformal.py` (etapa 4) precedía a `run_deep_pi.py` (etapa 5)."""
    vivo = _vivas(RUNBOOK)
    assert vivo.index("experiments/run_deep_pi.py") < vivo.index("experiments/improve_conformal.py")
    assert vivo.index("experiments/eval_deep_pi.py") < vivo.index("experiments/improve_conformal.py")


def test_deep_pi_sigue_siendo_best_effort_y_conformal_obligatorio() -> None:
    vivo = _vivas(RUNBOOK)
    assert re.search(r"^\s*run \$NF experiments/run_deep_pi\.py", vivo, re.M)
    assert re.search(r"^run_req \$ANTE experiments/improve_conformal\.py", vivo, re.M)


# ═══════════════════════════════ acreditación al leer
@pytest.fixture
def campana(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from vp_model import artifact_receipt as ar

    root = tmp_path / "repo"
    (root / "data" / "processed").mkdir(parents=True)
    (root / "data" / "processed" / "visa_panel_long.csv").write_text("a,b\n1,2\n")
    (root / "reports" / "eval").mkdir(parents=True)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run([*git, "init", "-q"], cwd=root, check=True)
    subprocess.run([*git, "add", "-A"], cwd=root, check=True)
    subprocess.run([*git, "commit", "-qm", "x", "--no-verify"], cwd=root, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
    panel = "sha256:" + ar.sha256_file(root / "data" / "processed" / "visa_panel_long.csv")
    (root / "reports" / "campaign").mkdir(parents=True)
    (root / "reports" / "campaign" / "campaign.json").write_text(
        json.dumps({"status": "running", "campaign_id": "rederiv_t_1", "source_git_sha": sha, "panel_sha256": panel})
    )
    monkeypatch.setenv("CAMPAIGN_ID", "rederiv_t_1")
    monkeypatch.setenv("CAMPAIGN_SHA", sha)
    return root, sha, panel


def _deep_pi(root: Path) -> Path:
    p = root / "reports" / "eval" / "deep_pi_FAD.csv"
    p.write_text("unique_id,ds,y\nmexico/family/F1,2025-01-01,1\n")
    return p


def _sellar(p: Path, cid: str, sha: str, panel: str) -> None:
    """Sella con el esquema y el protocolo DEL ESCRITOR: si lector y escritor divergen, el control cae."""
    pytest.importorskip("pandas")
    import run_deep_pi

    from vp_model import artifact_receipt as ar

    ar.seal(p, schema=run_deep_pi.RECEIPT_SCHEMA, campaign_id=cid, code_sha=sha, panel_sha256=panel,
            protocol=run_deep_pi.protocolo("FAD", False), coverage={})  # fmt: skip


def _lector():
    pytest.importorskip("darts")
    import improve_conformal

    return improve_conformal.deep_pi_no_acreditado


def test_RED_un_deep_pi_sin_recibo_no_es_baseline(campana) -> None:
    """La forma exacta de la cuarta campaña: había un CSV y nadie decía de qué corrida era."""
    root, _sha, _panel = campana
    motivo = _lector()(_deep_pi(root), "FAD", reports=root / "reports")
    assert motivo is not None and "recibo" in motivo


def test_un_deep_pi_de_otra_campana_no_es_baseline(campana) -> None:
    root, sha, panel = campana
    p = _deep_pi(root)
    _sellar(p, "rederiv_anterior", sha, panel)
    assert "otra corrida" in _lector()(p, "FAD", reports=root / "reports")


def test_control_benigno_el_deep_pi_de_esta_campana_si_es_baseline(campana) -> None:
    root, sha, panel = campana
    p = _deep_pi(root)
    _sellar(p, "rederiv_t_1", sha, panel)
    assert _lector()(p, "FAD", reports=root / "reports") is None


def test_un_deep_pi_tocado_despues_del_sellado_no_es_baseline(campana) -> None:
    root, sha, panel = campana
    p = _deep_pi(root)
    _sellar(p, "rederiv_t_1", sha, panel)
    p.write_text(p.read_text() + "mexico/family/F1,2025-02-01,2\n")
    assert "cambió desde el sellado" in _lector()(p, "FAD", reports=root / "reports")


def test_fuera_de_campana_deep_pi_no_se_sella(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sin transacción `running` no hay identidad que prestar: el CSV queda sin recibo, no con uno inventado."""
    pytest.importorskip("pandas")
    import argparse

    import run_deep_pi

    monkeypatch.delenv("CAMPAIGN_ID", raising=False)
    monkeypatch.setattr(run_deep_pi, "__file__", str(tmp_path / "experiments" / "run_deep_pi.py"))
    p = tmp_path / "deep_pi_FAD.csv"
    p.write_text("x\n")
    run_deep_pi._seal(p, argparse.Namespace(table="FAD", cqr=False, model="BiTCN"), [1])
    assert not (tmp_path / "deep_pi_FAD.csv.receipt.json").exists()
