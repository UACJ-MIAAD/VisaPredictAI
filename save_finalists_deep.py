"""Guarda los modelos GLOBALES profundos finalistas (neuralforecast) para reusar/comparar/graficar.

Por tabla (FAD, DFF) entrena sobre TODO el panel (encoding de régimen + primera diferencia,
la receta ganadora) y persiste cada modelo con ``nf.save()`` en ``models/{table}/global/{model}/``,
junto con sus pronósticos hold-out (24m, 1 paso, leakage-free) y una entrada en el manifiesto.
Corre en ``ante_nf``. Uso:  ante_nf/bin/python save_finalists_deep.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from run_global_deep import HOLDOUT, encode_regime, load_panel, regular_monthly  # noqa: F401

ROOT = Path(__file__).resolve().parent
MODELS = ROOT / "models"
MANIFEST = MODELS / "manifest.jsonl"
DET = ("BiTCN", "PatchTST", "TiDE", "NHITS")  # deterministas finalistas


def _diff(panel: pd.DataFrame) -> pd.DataFrame:
    parts = []
    for _u, g in panel.groupby("unique_id"):
        g = g.sort_values("ds").copy()
        g["y"] = g["y"].diff()
        parts.append(g.iloc[1:])
    return pd.concat(parts, ignore_index=True)


def _manifest(entry: dict) -> None:
    MODELS.mkdir(parents=True, exist_ok=True)
    with MANIFEST.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def main() -> None:
    import torch

    torch.set_num_threads(1)
    from neuralforecast import NeuralForecast
    from neuralforecast.auto import AutoBiTCN
    from neuralforecast.losses.pytorch import MAE
    from neuralforecast.models import NHITS, BiTCN, PatchTST, TiDE

    from run_global_deep import _auto_config, _optuna_sampler

    cls = {"BiTCN": BiTCN, "PatchTST": PatchTST, "TiDE": TiDE, "NHITS": NHITS}
    for table in ("FAD", "DFF"):
        panel = load_panel(table, "family")
        train = _diff(panel)
        inp = 36 if table == "FAD" else 18
        common = dict(
            h=1,
            input_size=inp,
            max_steps=800,
            scaler_type="standard",
            random_seed=1,
            enable_progress_bar=False,
            enable_model_summary=False,
        )
        builders = {m: (lambda M=cls[m], c=common: M(**c)) for m in DET}
        builders["AutoBiTCN"] = lambda: AutoBiTCN(
            h=1,
            loss=MAE(),
            config=_auto_config,
            num_samples=15,
            backend="optuna",
            search_alg=_optuna_sampler(1),
            verbose=False,
        )
        for name, build in builders.items():
            try:
                nf = NeuralForecast(models=[build()], freq="MS")
                nf.fit(train)
                out = MODELS / table / "global" / name
                out.mkdir(parents=True, exist_ok=True)
                nf.save(str(out), overwrite=True)
                _manifest(
                    {
                        "model": name,
                        "table": table,
                        "type": "global_deep",
                        "recipe": "diff+global+HPO" if name.startswith("Auto") else "diff+global",
                        "path": str(out.relative_to(ROOT)),
                        "n_series": int(panel["unique_id"].nunique()),
                    }
                )
                print(f"  ✓ {table}/{name} -> {out.relative_to(ROOT)}")
            except Exception as e:  # noqa: BLE001
                print(f"  ✗ {table}/{name} FALLO: {type(e).__name__}: {str(e)[:100]}")


if __name__ == "__main__":
    main()
