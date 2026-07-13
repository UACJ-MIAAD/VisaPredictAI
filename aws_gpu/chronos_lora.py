"""Chronos en el panel: zero-shot (reproduce el 0.225 del pool local) + fine-tune LoRA (GPU).

DOS partes:
  1. ZERO-SHOT (corre en CPU o GPU, REPRODUCIBLE): carga un pipeline Chronos-Bolt y predice
     el hold-out de 24m a 1 paso por serie, en NIVEL, leakage-free (solo contexto pasado).
     Escribe ``reports/global_{table}_chronos_zs.csv`` para evaluarlo con las métricas del
     proyecto. Es el baseline contra el que se compara el fine-tune (local: MASE 0.225 FAD).
  2. FINE-TUNE LoRA (GPU): adapta los pesos de Chronos al dominio con LoRA (peft) sobre el
     tramo de ENTRENAMIENTO (excluye los últimos 24m de cada serie → sin fuga), y reevalúa.

⚠️ La parte 2 depende de la API interna de ``chronos-forecasting`` + ``peft`` y NO se pudo
   validar sin GPU/instalación. El esqueleto sigue el flujo oficial de fine-tuning de Chronos
   (HF Trainer sobre ventanas contexto→objetivo); CONFIRMAR en la instancia antes de confiar
   en los números. La parte 1 sí está probada (data-prep con self-check).

Uso:
  python chronos_lora.py zeroshot --panel ../visa_panel_long.parquet --table FAD
  python chronos_lora.py finetune --panel ../visa_panel_long.parquet --table FAD --epochs 3
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

HOLDOUT = 24
PILOT = ("mexico", "india", "china", "philippines", "all_chargeability")
CONTEXT = 60  # longitud de contexto que recibe Chronos para predecir el siguiente mes
# Seguridad de supply chain (P0R, ronda 10): checkpoint canónico + revisión inmutable.
# ⚠️ CHRONOS_REVISION DEBE coincidir con vp_model.config.CHRONOS_REVISION — este bundle es
# STANDALONE (no importa vp_model), así que un test del repo principal verifica la igualdad.
DEFAULT_MODEL = "amazon/chronos-bolt-base"
CHRONOS_REVISION = "5d9f166d69f47aef3401367a7b842e78fe97b121"


def load_series(panel_path: str, table: str, block: str = "family") -> dict[str, np.ndarray]:
    """{unique_id: serie de nivel mensual regular} con el MISMO encoding de régimen que el deep.

    C = mes del boletín (Current = cutoff hoy), F = fecha de prioridad, U/UNK puenteados.
    Reusa ``encode_regime``/``regular_monthly`` de ``train_gpu`` (mismo bundle) para no divergir.
    """
    from train_gpu import encode_regime, regular_monthly  # mismo directorio del bundle

    df = pd.read_parquet(panel_path)
    df = df[(df["table"] == table) & (df["block"] == block) & (df["country"].isin(PILOT))].copy()
    df["unique_id"] = df["country"] + "/" + block + "/" + df["category"]
    df["ds"] = pd.to_datetime(df["bulletin_date"])
    out = {}
    min_len = (60 if table == "FAD" else 36) + HOLDOUT + 6
    for uid, g in df.groupby("unique_id"):
        s = regular_monthly(encode_regime(g[["ds", "status", "days_since_base"]]))
        if len(s) >= min_len:
            out[uid] = s.to_numpy(dtype="float64")
    return out


def holdout_windows(series: np.ndarray):
    """Genera (contexto, objetivo) para cada uno de los últimos 24 meses, 1 paso, sin fuga.

    Para el mes t del hold-out, el contexto son los <=CONTEXT valores ESTRICTAMENTE previos.
    """
    n = len(series)
    for i in range(n - HOLDOUT, n):
        ctx = series[max(0, i - CONTEXT) : i]
        yield i, ctx, series[i]


def zeroshot(args) -> None:
    """Predicción zero-shot a 1 paso sobre el hold-out; escribe CSV en NIVEL para evaluar."""
    import torch
    from chronos import BaseChronosPipeline

    # Hardening P0R: trust_remote_code=False + safetensors siempre; revisión inmutable para el
    # checkpoint canónico (misma política que vp_model.models.load_chronos_pipeline).
    kw = dict(
        device_map="cuda" if torch.cuda.is_available() else "cpu",
        torch_dtype=torch.bfloat16,
        trust_remote_code=False,
        use_safetensors=True,
    )
    if args.model == DEFAULT_MODEL:
        kw["revision"] = CHRONOS_REVISION
    pipe = BaseChronosPipeline.from_pretrained(args.model, **kw)
    rows = []
    for uid, s in load_series(args.panel, args.table).items():
        for i, ctx, y in holdout_windows(s):
            # API de chronos: el contexto es POSICIONAL (`inputs`), no `context=`; el primer
            # elemento del return son los cuantiles (el segundo es la media).
            q = pipe.predict_quantiles(
                torch.tensor(ctx, dtype=torch.float32),
                prediction_length=1,
                quantile_levels=[0.5],
            )[0]  # (1, 1) mediana
            yhat = float(np.asarray(q).reshape(-1)[0])
            rows.append({"unique_id": uid, "idx": i, "y": y, "Chronos": yhat})
    _write(rows, args.table, "chronos_zs", args.out_dir)


def finetune(args) -> None:  # noqa: D401
    """Fine-tune LoRA sobre el tramo de entrenamiento (sin los últimos 24m). GPU. NO validado.

    Flujo (a confirmar en la instancia): construir ventanas contexto→objetivo SOLO del tramo
    de entrenamiento, envolver el backbone de Chronos con peft.LoraConfig, entrenar con HF
    Trainer, y reusar ``zeroshot`` con el modelo adaptado para predecir el hold-out.
    """
    raise SystemExit(
        "finetune: esqueleto GPU. Pasos a implementar/validar en la instancia:\n"
        "  1. ventanas de ENTRENAMIENTO: series[:-24] → pares (contexto<=60, siguiente).\n"
        "  2. peft.LoraConfig(r=8, target_modules=['q','v']) sobre el T5 de Chronos-Bolt.\n"
        "  3. transformers.Trainer(epochs, lr~1e-4) — la pérdida es la del propio Chronos.\n"
        "  4. guardar adapter; reevaluar con la lógica de `zeroshot` (mismo CSV) y comparar\n"
        "     contra reports/global_FAD_chronos_zs.csv (zero-shot, ~0.225 local).\n"
        "Ver el repo oficial amazon-science/chronos-forecasting (scripts/training)."
    )


def _write(rows, table, suffix, out_dir) -> None:
    df = pd.DataFrame(rows)
    path = Path(out_dir) / f"chronos_{table}_{suffix}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    print(f"guardado {path} ({len(df)} filas, {df.unique_id.nunique()} series)")


def _selfcheck() -> None:
    """La ventana del hold-out es leakage-free: el contexto del mes t termina en t-1."""
    s = np.arange(100, dtype="float64")
    wins = list(holdout_windows(s))
    assert len(wins) == HOLDOUT
    for i, ctx, y in wins:
        assert y == s[i]
        assert len(ctx) > 0 and ctx[-1] == s[i - 1]  # último contexto = mes anterior, sin fuga
        assert ctx[-1] < y or i == 0
    print("selfcheck OK (ventanas hold-out sin fuga)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["zeroshot", "finetune", "selfcheck"])
    ap.add_argument("--panel", default="visa_panel_long.parquet")
    ap.add_argument("--table", default="FAD")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--out-dir", default="reports/campaign")
    ap.add_argument("--epochs", type=int, default=3)
    a = ap.parse_args()
    {"zeroshot": zeroshot, "finetune": finetune, "selfcheck": lambda _: _selfcheck()}[a.cmd](a)
