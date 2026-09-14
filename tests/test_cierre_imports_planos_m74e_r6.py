"""M74-E-R6 · el cierre sellado sigue los imports PLANOS entre módulos de `experiments/`.

`code_closure` sólo seguía imports cuyo primer segmento es una capa local (`vp_model.`, `tools.`…).
Los guiones de `experiments/` se importan entre sí por nombre plano, así que quedaban fuera del
sello: `seed_coverage.py` (que desde R6 escribe los sidecars que el agregador acredita),
`_figkit.py` y —anterior a R6— `hpo_winner_receipt.py`, que decide qué configuración ganadora
del HPO consume la campaña. Cambiar cualquiera de ellos no alteraba el preflight.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))


@pytest.mark.parametrize(
    "modulo", ["experiments/hpo_winner_receipt.py", "experiments/seed_coverage.py", "experiments/_figkit.py"]
)
def test_los_hermanos_importados_por_nombre_plano_quedan_sellados(modulo: str) -> None:
    from tools import campaign_preflight as pf

    assert modulo in pf.code_closure(pf.runbook_entrypoints()), f"{modulo} sigue fuera del sello"


def test_no_se_sella_un_tercero_con_nombre_plano() -> None:
    """Control: `import pandas` no es un hermano de `experiments/` y no debe resolverse como tal."""
    from tools import campaign_preflight as pf

    assert not [r for r in pf.code_closure(pf.runbook_entrypoints()) if r.endswith(("pandas.py", "numpy.py"))]
