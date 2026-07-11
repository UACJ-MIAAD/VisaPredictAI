"""Genera reports/governance/MODEL_CARD.md — tarjeta de modelo (estilo Google Model Cards) AUTO-derivada
de los artefactos canónicos, con un bloque de LINAJE reproducible.

Toda cifra sale de ``reports/governance/key_facts.json`` (la fuente única de verdad → la tarjeta queda
automáticamente alineada con el guardián de consistencia); la receta campeona, del manifiesto
y del veredicto campeón-retador; el linaje (git sha + hash del panel) se sella al generar.

    ante/bin/python experiments/build_model_card.py   (o `make model-card`)
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
sys.path.insert(0, str(ROOT))
from vp_data import tracking  # noqa: E402
from vp_model.config import HOLDOUT, MIN_TRAIN, MIN_TRAINABLE_EVALUABLE  # noqa: E402


def _load(name: str) -> dict:
    p = REPORTS / name
    return json.loads(p.read_text()) if p.exists() else {}


def _panel_hash() -> str:
    p = ROOT / "data" / "processed" / "visa_panel_long.csv"
    return hashlib.md5(p.read_bytes()).hexdigest()[:12] if p.exists() else "n/d"


def _release_id() -> str:
    # H3: el release VIGENTE al generar (el manifiesto de este corte se emite después,
    # en 4h — misma semántica que deployment_id en el ledger).
    from vp_model.ledger import current_release_id

    return current_release_id()


def _pipeline_run_id() -> str:
    return tracking.pipeline_run_id()


def _fmt(v) -> str:  # noqa: ANN001 — accepts int or the "n/d" degradation sentinel
    """Thousands-comma for ints only; the C1 degradation ("n/d") passes through.

    AP5: ``f"{v:,}"`` raised ValueError on the "n/d" fallback, so a missing
    key_facts.json crashed the very code path meant to degrade gracefully.
    """
    return f"{v:,}" if isinstance(v, int) else str(v)


def build() -> str:
    kf = _load("governance/key_facts.json")
    cc = _load("governance/champion_challenger.json")
    manifest = _load("governance/champion_manifest.json")
    sig = _load("eval/significance_summary.json")
    sha, _dirty = tracking.git_state()  # el flag dirty se omite: la tarjeta se genera pre-commit

    def recipe(table: str) -> str:
        r = manifest.get(table, {})
        models = r.get("models", [])
        agg = r.get("agg", "median")
        return models[0] if len(models) == 1 else f"{agg}({'+'.join(models)})"

    def champ_mean(table: str) -> str:
        return str(cc.get(table, {}).get("champion_mean", "n/d"))

    mcs = lambda t: ", ".join(sig.get("ranking", {}).get(t, {}).get("mcs_alpha10", []))  # noqa: E731

    md = f"""# Model Card — VisaPredict AI

> Tarjeta auto-generada por `experiments/build_model_card.py` a partir de los artefactos
> canónicos. **No editar a mano** — se regenera con `make model-card`. Toda cifra proviene de
> `reports/governance/key_facts.json` (fuente única de verdad).

## 1. Detalles del modelo
- **Sistema:** predictor del U.S. Visa Bulletin — panel multiserie `y_{{p,c,b,t}}` (país × categoría × tabla × mes).
- **Tarea:** regresión temporal de fechas de prioridad sobre observaciones con estado **F** (FAD y DFF por separado).
- **Receta desplegada (campeón):** FAD → `{recipe("FAD")}` · DFF → `{recipe("DFF")}` (manifiesto versionado `champion_manifest.json`).
- **Versión / linaje:** git `{sha}` · hash del panel `{_panel_hash()}`.
- **Autor:** Javier A. Rebull Saucedo · MIAAD, UACJ. Demostrador: visapredictai.com.

## 2. Uso previsto
- **Sí:** demostrar pronóstico con intervalos al 95 %/80 % para fines académicos y de exploración.
- **No:** asesoría legal/migratoria ni garantía de fechas. El sistema **no** predice cambios de régimen (C↔F↔U).

## 3. Factores
- País o área de cargabilidad: México, India, China, Filipinas, Resto del mundo.
- Categorías: familiares (F1–F4) y empleo (EB). Tablas: Final Action Dates (FAD) y Dates for Filing (DFF), evaluadas por separado.

## 4. Datos de entrenamiento
- **Panel:** {_fmt(kf.get("n_obs", "n/d"))} observaciones · {kf.get("pct_trainable_F", "n/d")} % entrenables (estado F = {_fmt(kf.get("n_obs_F", "n/d"))}) · rango {kf.get("date_first", "?")} → {kf.get("date_last", "?")}.
- **Series:** {kf.get("n_series_structural", "n/d")} estructurales · {kf.get("n_series_evaluable", "n/d")} plenamente evaluables (≥{MIN_TRAINABLE_EVALUABLE} obs F = ventana {MIN_TRAIN["FAD"]} + hold-out {HOLDOUT}).
- Fuente: U.S. Department of State, Visa Bulletin (HTML congelado, parseo offline reproducible).

## 5. Evaluación
**Marco comparativo:** {kf.get("n_models", "n/d")} modelos evaluados bajo el mismo protocolo walk-forward; de ahí salen el campeón desplegado y su retador.
**Hold-out leakage-free (MASE media):** FAD campeón `{recipe("FAD")}` = **{champ_mean("FAD")}** · DFF campeón `{recipe("DFF")}` = **{champ_mean("DFF")}**.
**Model Confidence Set (90 %):** FAD = {{{mcs("FAD")}}} · DFF = {{{mcs("DFF")}}} (Friedman–Nemenyi).
**Prospectiva (backfill sin fuga de información; añadas servidas en vivo desde jul-2026):** n={kf.get("prosp_n_scored", "n/d")} · MAE={kf.get("prosp_mae_days", "n/d")} días · MASE={kf.get("prosp_mase", "n/d")} · cobertura 95 %={kf.get("prosp_cov95", "n/d")} · 80 % (out-of-sample)={kf.get("prosp_cov80_heldout", "n/d")}.

## 6. Linaje y reproducibilidad
- **Receta:** `champion_manifest.json` (cambia solo vía `run_champion_challenger.py --promote`, auditado).
- **Código:** git `{sha}`. **Datos:** panel hash `{_panel_hash()}`. **Pipeline:** `dvc repro` (DAG determinista, `dvc.lock`).
- **Corte (H3):** release vigente al generar `{_release_id()}` · pipeline_run_id `{_pipeline_run_id()}` · añada `{kf.get("panel_vintage", "n/d")}`.
- **Promoción (dos gates):** el hold-out (Wilcoxon+Holm, h=1) solo declara aptitud retrospectiva; la autorización la da el gate prospectivo PRE-REGISTRADO (docs/PROMOTION_POLICY.md) sobre pares live campeón-vs-sombra, aplicada por un humano (`--promote`, que se rehúsa sin decisión "promote") con rollback versionado.

## 7. Limitaciones y consideraciones éticas
- El borde del modelado profundo sobre los clásicos es **modesto y frágil** (sensible a agregación; muestra DFF efectiva pequeña).
- Solo modela estado F; C/U son anotación descriptiva, no objetivo.
- Las retrogresiones por cuota son reales y el modelo debe tolerarlas; no constituye consejo legal.
- El registro prospectivo actual es un backfill sin fuga (información truncada al origen), no pronósticos servidos en tiempo real; las añadas servidas en vivo se acumulan desde jul-2026.
"""
    (REPORTS / "governance").mkdir(parents=True, exist_ok=True)
    (REPORTS / "governance" / "MODEL_CARD.md").write_text(md)
    return md


if __name__ == "__main__":
    build()
    print(f"escrito {REPORTS / 'governance' / 'MODEL_CARD.md'}")
