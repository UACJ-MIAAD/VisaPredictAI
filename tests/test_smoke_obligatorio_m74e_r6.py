"""M74-E-R6 · el smoke de entrypoints es PUERTA del runbook, antes del primer artefacto (B2).

`tools/check_entrypoint_smoke.py` detecta en ~2 minutos la clase de defecto que costó once horas,
pero el runbook sólo lo mencionaba en un comentario: dependía de la memoria del operador, y por
eso tampoco entraba al cierre de código sellado. Decisión del autor: invocación fail-closed.
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


def _vivas() -> str:
    return "\n".join(ln for ln in RUNBOOK.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#"))


def test_el_smoke_se_invoca_antes_del_primer_artefacto() -> None:
    vivo = _vivas()
    assert '"$ANTE" tools/check_entrypoint_smoke.py' in vivo, "el runbook no invoca el smoke"
    i = vivo.index("tools/check_entrypoint_smoke.py")
    assert vivo.index("tools.check_env_matches_lock") < i, "debe ir DESPUÉS de acreditar los entornos"
    for artefacto in ("campaign_manifest.json", "txn archive", "txn open", "mkdir -p reports/campaign"):
        assert i < vivo.index(artefacto), f"el smoke corre después de {artefacto!r}"


def test_el_smoke_entra_al_cierre_de_codigo_sellado() -> None:
    """Un gate fuera del sello puede cambiar sin que el sello se entere."""
    from tools import campaign_preflight as pf

    eps = pf.runbook_entrypoints()
    assert "tools/check_entrypoint_smoke.py" in eps["scripts"]
    assert "tools/check_entrypoint_smoke.py" in pf.code_closure(eps)


def _stub(ruta: Path) -> None:
    ruta.parent.mkdir(parents=True)
    ruta.write_text(
        '#!/bin/sh\ncase "$*" in *check_entrypoint_smoke*) [ "${SMOKE_FALLA:-0}" = 1 ] && exit 1;; esac\nexit 0\n',
        encoding="utf-8",
    )
    ruta.chmod(ruta.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.mark.parametrize("falla,rc,artefacto", [(True, 9, False), (False, 0, True)])
def test_RED_un_smoke_roto_impide_crear_el_primer_artefacto(
    tmp_path: Path, falla: bool, rc: int, artefacto: bool
) -> None:
    """★ Conductual sobre el texto REAL: el bloque de arranque del runbook, con intérpretes de mentira.

    Tras el bloque se añade la creación del primer artefacto. Con el smoke roto no debe existir;
    con el smoke sano (control) sí.
    """
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
    fin = subprocess.run(
        ["bash", str(sh)],
        capture_output=True,
        text=True,
        timeout=120,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "SMOKE_FALLA": "1" if falla else "0"},
    )
    assert fin.returncode == rc, fin.stderr
    assert (tmp_path / "reports" / "campaign" / "campaign_manifest.json").exists() is artefacto
