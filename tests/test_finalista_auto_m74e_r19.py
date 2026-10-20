"""M74-E-R19 · el finalista AutoBiTCN se ajusta con la cola de validación que su ganadora exige.

La primera campaña real sobre `bcaeb03` (`rederiv_bcaeb03_20260915T230637`, 16-sep-2026) pasó la etapa 1 entera
(13 h 39 min) y murió en [2b/4]: el promotor contó 8 de 10 globales. `save_finalists_deep.py` reconstruye AutoBiTCN
desde la ganadora sellada del HPO —que trae `early_stop_patience_steps`/`val_check_steps` (AK8b)— y llamaba
`nf.fit(train)` sin cola de validación, así que NeuralForecast lanzaba «Set val_size>0 or provide a val_df if early
stopping is enabled»; el productor tragaba la excepción por modelo y seguía en verde hasta que el promotor lo paró
una hora después. El runner (`run_global_deep.py`) pasa `VAL_SIZE` para `--auto`/`--config`: seis auditorías
ciegas ejecutaron ese runner y ninguna ejecutó ESTE ajuste con una ganadora real.

Aquí el ajuste corre DE VERDAD en el intérprete deep (`ante_nf`), sobre un panel sintético minúsculo y con una
configuración de la misma forma que la ganadora real; y un global fallido cierra el productor.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "experiments"))
NF = RAIZ / "ante_nf" / "bin" / "python"

#: Misma forma que `reports/campaign/hpo_deep_best_FAD_AutoBiTCN.json` (claves reales), tamaños mínimos.
GANADORA = {
    "input_size": 6,
    "learning_rate": 1e-3,
    "scaler_type": "robust",
    "max_steps": 4,
    "early_stop_patience_steps": 1,
    "val_check_steps": 2,
    "logger": False,
    "accelerator": "cpu",
    "hidden_size": 4,
    "dropout": 0.1,
}

AJUSTE = r"""
import json, sys, warnings
import numpy as np, pandas as pd
warnings.simplefilter("ignore")
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/experiments")
from hpo_winner_receipt import build_finalist
from neuralforecast import NeuralForecast
from neuralforecast.models import BiTCN
import save_finalists_deep as sfd
cfg = json.loads(sys.argv[2])
ds = pd.date_range("2018-01-01", periods=48, freq="MS")
rng = np.random.default_rng(0)
train = pd.concat(
    pd.DataFrame({"unique_id": uid, "ds": ds, "y": rng.normal(size=48)}) for uid in ("a", "b")
).reset_index(drop=True)
modo = sys.argv[3]
nf = NeuralForecast(models=[build_finalist(BiTCN, cfg)], freq="MS")
if modo == "sin_cola":
    nf.fit(train)  # lo que hacia el productor: revienta con early stopping
else:
    sfd.fit_finalist(nf, train, "AutoBiTCN")
print("AJUSTADO", len(nf.models))
"""


def _ajuste(modo: str, home: Path) -> subprocess.CompletedProcess[str]:
    """`HOME` es un temporal: matplotlib y Lightning escriben caches bajo HOME y el arbol debe quedar limpio."""
    import json

    return subprocess.run(
        [str(NF), "-c", AJUSTE, str(RAIZ), json.dumps(GANADORA), modo],
        capture_output=True,
        text=True,
        timeout=600,
        env={"PATH": "/usr/bin:/bin", "HOME": str(home), "VP_DEEP_ACCEL": "cpu", "PYTHONDONTWRITEBYTECODE": "1"},
    )


@pytest.mark.skipif(not NF.exists(), reason="entorno deep ante_nf ausente")
def test_RED_el_finalista_auto_se_ajusta_de_verdad_con_la_cola_del_runner(tmp_path: Path) -> None:
    """Contra `bcaeb03`: `fit_finalist` no existe (AttributeError) — el productor llamaba `nf.fit(train)`."""
    fin = _ajuste("con_cola", tmp_path)
    assert fin.returncode == 0 and "AJUSTADO 1" in fin.stdout, fin.stderr[-600:]


@pytest.mark.skipif(not NF.exists(), reason="entorno deep ante_nf ausente")
def test_control_el_ajuste_sin_cola_es_exactamente_el_fallo_de_la_campana(tmp_path: Path) -> None:
    """Sin cola, NeuralForecast rechaza el early stopping: es el mensaje literal de la bitácora del 16-sep."""
    fin = _ajuste("sin_cola", tmp_path)
    assert fin.returncode != 0 and "val_size" in fin.stderr, fin.stderr[-600:]


def test_RED_un_global_fallido_cierra_el_productor_antes_de_los_locales() -> None:
    import save_finalists_deep as sfd

    sfd._cerrar([])
    with pytest.raises(SystemExit, match="FAD/AutoBiTCN"):
        sfd._cerrar(["FAD/AutoBiTCN"])


def test_los_deterministas_siguen_sin_cola_y_los_auto_llevan_la_del_runner() -> None:
    import run_global_deep as rgd
    import save_finalists_deep as sfd

    class NF:
        def __init__(self) -> None:
            self.llamadas: list[int] = []

        def fit(self, train: object, val_size: int = 0) -> None:
            self.llamadas.append(val_size)

    nf = NF()
    sfd.fit_finalist(nf, None, "BiTCN")
    sfd.fit_finalist(nf, None, "AutoBiTCN")
    assert nf.llamadas == [0, rgd.VAL_SIZE] and rgd.VAL_SIZE > 0
