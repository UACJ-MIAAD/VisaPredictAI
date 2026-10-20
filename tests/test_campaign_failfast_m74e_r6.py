"""M74-E-R6 · `run_campaign.sh` se detiene en el primer productor roto (auditoría `8bc41ff6…`, B1).

La auditoría ejecutó la semántica vieja de `step` con un productor que devolvía 17: el consumidor
posterior corrió igual (`fails=1 events=consumer-ran`) y el script sólo salió rojo al final. En la
campaña eso significa que la agregación multi-semilla leía por glob `global_*.csv` de otra corrida
y `sync_all` los re-hasheaba antes del rojo.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
CAMPANA = RAIZ / "experiments" / "run_campaign.sh"


def _step_real(texto: str | None = None) -> str:
    """La definición REAL de `step`, recortada del guion: se ejecuta tal cual, no se parafrasea.

    ⚠️ Admite la forma de UNA línea (la de `af84350`) y la multilínea: exigir sólo la nueva hacía
    que la prueba fallara contra el commit viejo por no encontrar la función, no por su conducta,
    y esa «discriminación» no medía nada.
    """
    lineas = (texto if texto is not None else CAMPANA.read_text(encoding="utf-8")).splitlines()
    i = next(k for k, ln in enumerate(lineas) if ln.startswith("step() {"))
    if lineas[i].rstrip().endswith("}"):
        return lineas[i] + "\n"
    j = next(k for k in range(i + 1, len(lineas)) if lineas[k] == "}")
    return "\n".join(lineas[i : j + 1]) + "\n"


@pytest.mark.parametrize("productor,rc,consumidor_corre", [("exit 17", 17, False), ("true", 0, True)])
def test_RED_tras_un_productor_roto_no_corren_ni_agregacion_ni_sync(
    tmp_path: Path, productor, rc, consumidor_corre
) -> None:
    guion = tmp_path / "campana.sh"
    guion.write_text(
        "#!/bin/bash\nset -uo pipefail\n"
        + _step_real()
        + f'step "semilla deep" bash -c "{productor}"\n'
        + f'step "agregación" touch "{tmp_path}/agregacion"\n'
        + f'step "sync_all" touch "{tmp_path}/sync"\n'
        + "exit 0\n",
        encoding="utf-8",
    )
    fin = subprocess.run(["bash", str(guion)], capture_output=True, text=True, timeout=60)
    assert fin.returncode == rc, fin.stderr
    assert (tmp_path / "agregacion").exists() is consumidor_corre
    assert (tmp_path / "sync").exists() is consumidor_corre


def test_no_queda_contabilidad_acumulativa_de_fallos() -> None:
    """Un contador que se consulta al final es la semántica que dejaba seguir."""
    vivas = [ln for ln in CAMPANA.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#")]
    assert not [ln for ln in vivas if "CAMP_FAILS" in ln]
