"""M74-E-R8 · la ruta real de lanzamiento impone la procedencia y el sello de entradas.

Auditoría `e6c76896…`, bloqueo 3: ni el runbook ni el preflight ejecutaban el contrato de procedencia
de locks, así que un preflight doble verde convivía con un checkout cuyo contrato fallaba; y el
preflight que la campaña debía consumir era un archivo manual que nadie le pasaba.
"""

from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
RUNBOOK = RAIZ / "experiments" / "run_rederivation.sh"


# ═══════════════════════════════ bloqueo 3 · el preflight impone la procedencia y el runbook lo impone todo
def test_RED_el_preflight_no_sella_sobre_una_base_git_ausente(tmp_path: Path, monkeypatch, capsys) -> None:
    """★ Con la base del lockset ausente de git el preflight falla y NO escribe sello. Antes salía verde."""
    from tools import campaign_preflight as pf
    from tools import lock_contracts as lc

    real = lc._git
    monkeypatch.setattr(lc, "_git", lambda repo, *args: None if args[:2] == ("cat-file", "-e") else real(repo, *args))
    salida = tmp_path / "preflight.json"
    assert pf.main(["--out", str(salida)]) == 1
    assert not salida.exists()
    assert "NO existe como commit" in capsys.readouterr().err


def test_el_lockset_y_el_contrato_entran_en_el_sello() -> None:
    from tools import campaign_preflight as pf

    assert "locks/lockset.json" in pf.GOVERNANCE_PLAIN
    assert "tools/lock_contracts.py" in pf.code_closure(pf.runbook_entrypoints())


def _vivas() -> str:
    return "\n".join(ln for ln in RUNBOOK.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#"))


def test_contrato_y_preflight_corren_antes_del_primer_artefacto() -> None:
    vivo = _vivas()
    i_locks, i_pre = vivo.index("-m tools.lock_contracts"), vivo.index("-m tools.campaign_preflight")
    assert vivo.index("tools/check_entrypoint_smoke.py") < i_locks < i_pre
    for artefacto in ("mkdir -p reports/campaign", "campaign_manifest.json", "txn archive", "txn open"):
        assert i_pre < vivo.index(artefacto), f"el preflight corre después de {artefacto!r}"
    assert "preflight_sha256" in vivo[vivo.index("campaign_manifest.json") - 400 : vivo.index("campaign_manifest.json")]


def _stub(ruta: Path) -> None:
    ruta.parent.mkdir(parents=True)
    ruta.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *lock_contracts*) [ "${LOCKS_FALLA:-0}" = 1 ] && exit 1;;\n'
        '  *campaign_preflight*) [ "${PREFLIGHT_FALLA:-0}" = 1 ] && exit 1\n'
        '    previo=""; for a in "$@"; do [ "$previo" = --out ] && echo "{}" > "$a"; previo="$a"; done;;\n'
        "esac\nexit 0\n",
        encoding="utf-8",
    )
    ruta.chmod(ruta.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.mark.parametrize(
    "falla,rc,artefacto",
    [("LOCKS_FALLA", 10, False), ("PREFLIGHT_FALLA", 11, False), ("NINGUNA", 0, True)],
)
def test_RED_una_procedencia_o_un_preflight_rotos_impiden_el_primer_artefacto(
    tmp_path: Path, falla: str, rc: int, artefacto: bool
) -> None:
    """★ Conductual sobre el bloque de arranque REAL, con intérpretes de mentira (patrón de R6)."""
    guion = RUNBOOK.read_text(encoding="utf-8")
    bloque = guion[guion.index("set -uo pipefail") : guion.index("tree_dirty() {")]
    (tmp_path / "experiments").mkdir()
    sh = tmp_path / "experiments" / "arranque.sh"
    sh.write_text(
        "#!/bin/bash\n"
        + bloque
        + "\nmkdir -p reports/campaign && : > reports/campaign/campaign_manifest.json\nexit 0\n",
        encoding="utf-8",
    )
    _stub(tmp_path / "ante" / "bin" / "python")
    _stub(tmp_path / "ante_nf" / "bin" / "python")
    entorno = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path), falla: "1"}
    fin = subprocess.run(["bash", str(sh)], capture_output=True, text=True, timeout=120, env=entorno)
    assert fin.returncode == rc, fin.stderr
    assert (tmp_path / "reports" / "campaign" / "campaign_manifest.json").exists() is artefacto
