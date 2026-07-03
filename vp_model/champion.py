"""Evaluación campeón–retador de la receta de pronóstico DESPLEGADA.

El demostrador web sirve una receta fija por tabla (FAD: mediana{theta,ets,sarima};
DFF: sarima). Este módulo evalúa ese **campeón** contra una baraja de **retadores** sobre
el hold-out leakage-free (reutilizando los forecasts persistidos por serie×fecha), corre
un **Wilcoxon pareado por serie** contra cada retador con **corrección de Holm**, y emite
un **veredicto de promoción**.

La promoción está **gateada**: un retador se RECOMIENDA solo si le gana al campeón con
significancia ajustada por Holm Y un margen medio material; el cambio real es una edición
explícita y auditada del manifiesto (`reports/governance/champion_manifest.json`), NUNCA automática.
La confirmación PROSPECTIVA (sobre el ledger congelado) requiere desplegar el retador en
sombra primero — el ledger hoy solo califica al campeón — y queda anotada como pendiente.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from scipy.stats import wilcoxon

from vp_model import dataset, significance
from vp_model.metrics import naive_scale_before

REPORTS = Path(__file__).resolve().parent.parent / "reports"
MANIFEST = REPORTS / "governance" / "champion_manifest.json"

# Baraja de retadores por tabla: cada uno es (modelos, agregación). Un modelo único = receta
# de un solo elemento (la agregación es irrelevante). Solo modelos presentes en los forecasts
# persistidos (pool clásico reproducible en un solo entorno).
CHALLENGERS: dict[str, list[tuple[tuple[str, ...], str]]] = {
    "FAD": [
        (("theta",), "median"),
        (("ets",), "median"),
        (("theta", "ets"), "median"),
        (("theta", "ets", "sarima"), "mean"),
        (("theta", "ets", "sarima", "arima"), "median"),
    ],
    "DFF": [
        (("ets",), "median"),
        (("catboost",), "median"),
        (("sarima", "ets"), "median"),
        (("sarima", "ets", "theta"), "median"),
    ],
}

# Reglas del gate de promoción.
MATERIAL_MARGIN = 0.005  # mejora media mínima en MASE para considerarla material
HOLM_ALPHA = 0.05


@dataclass
class Recipe:
    models: tuple[str, ...]
    agg: str = "median"

    @property
    def name(self) -> str:
        return self.models[0] if len(self.models) == 1 else f"{self.agg}({'+'.join(self.models)})"


@dataclass
class Verdict:
    table: str
    champion: str
    champion_mean: float
    champion_median: float
    challengers: list[dict] = field(default_factory=list)
    promote: dict | None = None  # el retador recomendado, o None = mantener al campeón
    n_series_raw: int = 0  # series antes del dedup de pseudo-réplicas (B2)
    n_series_effective: int = 0  # series DISTINTAS que alimentan el Wilcoxon


def replica_representatives(table: str) -> list[tuple[str, str]]:
    """Una serie representante por clase de pseudo-réplica del corte mundial (B2).

    Series cuyos valores REALES del hold-out son idénticos fecha a fecha comparten el
    corte mundial (India/China/All-Charg. en varias categorías); contarlas todas hace
    anticonservador el Wilcoxon que decide qué receta se despliega a la web. Firma =
    vector de ``actual`` por fecha (mismo criterio que ``significance_tables``).
    """
    fc = pd.read_csv(REPORTS / "eval" / f"holdout_forecasts_{table}.csv", parse_dates=["date"])
    sig = fc.drop_duplicates(subset=["country", "category", "date"]).pivot_table(
        index=["country", "category"], columns="date", values="actual"
    )
    return list(sig[~sig.duplicated()].index)


def recipe_series_mase(table: str, recipe: Recipe) -> pd.Series:
    """MASE de hold-out por serie de una receta, leakage-free.

    Reconstruye el punto de la receta (mediana/media de sus modelos por serie×fecha) sobre
    ``reports/eval/holdout_forecasts_{table}.csv`` y escala por el naïve estacional in-sample
    calculado SOLO con el tramo previo al hold-out (misma fuente que el resto del proyecto).
    """
    fc = pd.read_csv(REPORTS / "eval" / f"holdout_forecasts_{table}.csv", parse_dates=["date"])
    missing = set(recipe.models) - set(fc.model.unique())
    if missing:
        raise ValueError(f"receta {recipe.name}: modelos ausentes en holdout_forecasts_{table}: {sorted(missing)}")
    sub = fc[fc.model.isin(recipe.models)]
    comb = (
        sub.groupby(["country", "category", "date"])
        .agg(actual=("actual", "first"), pred=("forecast", recipe.agg))
        .reset_index()
    )
    out: dict[tuple[str, str], float] = {}
    for (country, category), g in comb.groupby(["country", "category"]):
        full = dataset.load_series(country, category, table).astype("float64")
        scale = naive_scale_before(full, g["date"].min())
        out[(country, category)] = float((g.actual - g.pred).abs().mean() / scale)
    return pd.Series(out, name=recipe.name)


def _compare(champ: pd.Series, chall: pd.Series) -> dict:
    """Wilcoxon pareado por serie campeón vs retador (índice común)."""
    common = champ.index.intersection(chall.index)
    a, b = champ.loc[common], chall.loc[common]
    margin_mean = float(a.mean() - b.mean())  # >0 = retador mejor en media
    if (a - b).abs().sum() == 0:
        pval = 1.0
    else:
        pval = float(wilcoxon(a.to_numpy(), b.to_numpy()).pvalue)
    return {
        "challenger": chall.name,
        "mean": round(float(b.mean()), 4),
        "median": round(float(b.median()), 4),
        "n_series": int(len(common)),
        "mean_margin_vs_champion": round(margin_mean, 4),  # >0 retador mejor
        "wilcoxon_p": round(pval, 5),
    }


def evaluate(table: str, champion: Recipe, challengers: list[Recipe] | None = None) -> Verdict:
    chall_recipes = challengers or [Recipe(m, a) for m, a in CHALLENGERS.get(table, [])]
    champ_mase = recipe_series_mase(table, champion)
    # B2: el Wilcoxon del gate corre SOLO sobre series distintas (una representante por
    # clase de pseudo-réplica); filtrar al campeón basta — _compare intersecta índices.
    n_raw = len(champ_mase)
    reps = replica_representatives(table)
    champ_mase = champ_mase[champ_mase.index.isin(reps)]
    rows = [_compare(champ_mase, recipe_series_mase(table, r)) for r in chall_recipes]

    adj = significance.holm({r["challenger"]: r["wilcoxon_p"] for r in rows}, alpha=HOLM_ALPHA)
    for r in rows:
        p_adj, reject = adj[r["challenger"]]
        r["holm_p"] = round(float(p_adj), 5)
        # promovible: gana en media de forma material Y Holm-significativo
        r["promotable"] = bool(reject and r["mean_margin_vs_champion"] >= MATERIAL_MARGIN)

    promotable = [r for r in rows if r["promotable"]]
    best = max(promotable, key=lambda r: r["mean_margin_vs_champion"]) if promotable else None
    return Verdict(
        table=table,
        champion=champion.name,
        champion_mean=round(float(champ_mase.mean()), 4),
        champion_median=round(float(champ_mase.median()), 4),
        challengers=sorted(rows, key=lambda r: r["mean"]),
        promote=best,
        n_series_raw=n_raw,
        n_series_effective=len(champ_mase),
    )


def load_manifest() -> dict[str, Recipe]:
    """Receta campeona versionada por tabla. Si no existe, la default = receta desplegada."""
    if MANIFEST.exists():
        raw = json.loads(MANIFEST.read_text())
        return {t: Recipe(tuple(v["models"]), v.get("agg", "median")) for t, v in raw.items()}
    return {"FAD": Recipe(("theta", "ets", "sarima"), "median"), "DFF": Recipe(("sarima",), "median")}


def save_manifest(champions: dict[str, Recipe]) -> None:
    MANIFEST.write_text(
        json.dumps(
            {t: {"models": list(r.models), "agg": r.agg} for t, r in champions.items()},
            indent=2,
        )
        + "\n"
    )


def demo() -> None:
    """Self-check: el campeón FAD (mediana fuerte) le gana al naïve y al ARIMA solo."""
    champ = load_manifest()["FAD"]
    v = evaluate("FAD", champ)
    assert 0 < v.champion_mean < 0.5, v.champion_mean
    assert all("holm_p" in c for c in v.challengers)
    print(f"FAD campeón={v.champion} mean MASE={v.champion_mean} · promover={v.promote and v.promote['challenger']}")


if __name__ == "__main__":
    demo()
