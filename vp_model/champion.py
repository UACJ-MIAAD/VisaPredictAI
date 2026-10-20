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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
from scipy.stats import wilcoxon

from vp_model import dataset, significance
from vp_model.config import PROBABILISTIC, RANDOM_SEED
from vp_model.metrics import naive_scale_before

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
MANIFEST = REPORTS / "governance" / "champion_manifest.json"

# Baraja de retadores por tabla: cada uno es (modelos, agregación). Un modelo único = receta
# de un solo elemento (la agregación es irrelevante). Solo modelos presentes en los forecasts
# persistidos (pool clásico reproducible en un solo entorno).
CHALLENGERS: dict[str, list[tuple[tuple[str, ...], str]]] = {
    "FAD": [
        (("naive1",), "median"),  # AQ: the RW won both MCS at h=1 — governance must track it
        (("drift",), "median"),  # 7-jul: rolling multi-h champion; tracked here to record it LOSES to naive1 at h=1
        (("theta",), "median"),
        (("ets",), "median"),
        (("theta", "ets"), "median"),
        (("theta", "ets", "sarima"), "mean"),
        (("theta", "ets", "sarima", "arima"), "median"),
    ],
    "DFF": [
        (("naive1",), "median"),  # AQ: sole MCS member at h=1
        (("drift",), "median"),  # 7-jul: rolling multi-h champion; loses to naive1 at h=1 (regime-dependent)
        (("ets",), "median"),
        (("catboost",), "median"),
        (("sarima", "ets"), "median"),
        (("sarima", "ets", "theta"), "median"),
    ],
}

# Reglas del gate de promoción.
MATERIAL_MARGIN = 0.005  # mejora media mínima en MASE para considerarla material
HOLM_ALPHA = 0.05

#: Baraja pre-registrada (E4). El diccionario ``CHALLENGERS`` de arriba se conserva como
#: respaldo legible, pero la ÚNICA lectura autorizada es :func:`load_deck`, que además trae el
#: pre-registro del router y falla cerrado si el archivo no cuadra con el código.
DECK_PATH = ROOT / "docs" / "challenger_deck.json"


@dataclass
class Recipe:
    models: tuple[str, ...]
    agg: str = "median"

    @property
    def name(self) -> str:
        return self.models[0] if len(self.models) == 1 else f"{self.agg}({'+'.join(self.models)})"


def load_deck(ruta: Path | None = None) -> dict:
    """Lee y VALIDA la baraja pre-registrada. Única puerta de lectura.

    Fail-closed: si el archivo no declara el margen material, el alfa de Holm, los horizontes o
    los candidatos del router —o si contradice lo que el código tiene congelado desde julio—, no
    se devuelve nada. Una baraja que se pueda editar sin que nadie lo note no es un pre-registro.
    """
    from vp_model.config import HORIZON_CANDIDATES, HORIZONS

    crudo = json.loads((ruta or DECK_PATH).read_text())
    faltan = {"version", "challengers", "gate", "router", "pool", "inputs"} - set(crudo)
    if faltan:
        raise ValueError(f"la baraja no declara {sorted(faltan)}")
    gate, router = crudo["gate"], crudo["router"]
    if gate["material_margin"] != MATERIAL_MARGIN:
        raise ValueError(f"margen material {gate['material_margin']} != {MATERIAL_MARGIN} del código")
    if gate["holm_alpha"] != HOLM_ALPHA:
        raise ValueError(f"alfa de Holm {gate['holm_alpha']} != {HOLM_ALPHA} del código")
    if "BILATERAL" not in gate["test"].upper():
        raise ValueError("el contraste primario declarado no es bilateral")
    if tuple(router["candidates"]) != tuple(HORIZON_CANDIDATES):
        raise ValueError("los candidatos del router no son los congelados en config.HORIZON_CANDIDATES")
    if not set(router["horizons"]) <= set(HORIZONS):
        raise ValueError("los horizontes del router no están en config.HORIZONS")
    if router["allowed_material_loss"] != 0.0:
        raise ValueError("la pérdida material permitida declarada no es cero")
    return crudo


def deck_challengers(table: str, ruta: Path | None = None) -> list[Recipe]:
    """Retadores de una tabla, leídos por la única puerta."""
    return [recipe_from_dict(d) for d in load_deck(ruta)["challengers"].get(table, [])]


def recipe_from_dict(d: dict) -> Recipe:
    """Rebuild a Recipe from its serialized form (AP4: the display name is for humans only)."""
    return Recipe(tuple(d["models"]), d.get("agg", "median"))


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


def load_holdout_forecasts(table: str) -> pd.DataFrame:
    """Pronósticos de hold-out ACREDITADOS (AP5: se cargan UNA vez por evaluate, no 7).

    ★ M74-E-R1: pasa por la puerta acreditada. Antes leía el archivo que hubiera, y el campeón
    se decidía sobre él.
    """
    from vp_model import persist_forecasts

    return persist_forecasts.read_accredited(table, reports=REPORTS)


def replica_representatives(table: str, fc: pd.DataFrame | None = None) -> list[tuple[str, str]]:
    """Una serie representante por clase de pseudo-réplica del corte mundial (B2).

    Series cuyos valores REALES del hold-out son idénticos fecha a fecha comparten el
    corte mundial (India/China/All-Charg. en varias categorías); contarlas todas hace
    anticonservador el Wilcoxon que decide qué receta se despliega a la web. Firma =
    vector de ``actual`` por fecha (mismo criterio que ``significance_tables``).
    """
    if fc is None:
        fc = load_holdout_forecasts(table)
    sig = fc.drop_duplicates(subset=["country", "category", "date"]).pivot_table(
        index=["country", "category"], columns="date", values="actual"
    )
    return list(sig[~sig.duplicated()].index)


def recipe_series_mase(table: str, recipe: Recipe, fc: pd.DataFrame | None = None) -> pd.Series:
    """MASE de hold-out por serie de una receta, leakage-free.

    Reconstruye el punto de la receta (mediana/media de sus modelos por serie×fecha) sobre
    ``reports/eval/holdout_forecasts_{table}.csv`` y escala por el naïve estacional in-sample
    calculado SOLO con el tramo previo al hold-out (misma fuente que el resto del proyecto).
    ``fc`` acepta el frame ya cargado (``load_holdout_forecasts``) para no releer el CSV.
    """
    if fc is None:
        fc = load_holdout_forecasts(table)
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
    fc = load_holdout_forecasts(table)  # AP5: one read serves champion + all challengers + B2
    champ_mase = recipe_series_mase(table, champion, fc)
    # B2: el Wilcoxon del gate corre SOLO sobre series distintas (una representante por
    # clase de pseudo-réplica); filtrar al campeón basta — _compare intersecta índices.
    n_raw = len(champ_mase)
    reps = replica_representatives(table, fc)
    champ_mase = champ_mase[champ_mase.index.isin(reps)]
    rows = []
    for rec in chall_recipes:
        row = _compare(champ_mase, recipe_series_mase(table, rec, fc))
        # AP4: carry the serialized Recipe so promotion/shadow never re-parse the display name.
        row["recipe"] = asdict(rec)
        rows.append(row)

    adj = significance.holm({r["challenger"]: r["wilcoxon_p"] for r in rows}, alpha=HOLM_ALPHA)
    for r in rows:
        p_adj, reject = adj[r["challenger"]]
        r["holm_p"] = round(float(p_adj), 5)
        # apto en HOLD-OUT (h=1 retrospectivo): gana en media de forma material Y es
        # Holm-significativo. A4: NO autoriza producción (el nombre viejo "promotable"
        # lo sugería) — la autorización la da el gate prospectivo pre-registrado
        # (vp_model/promotion.py) sobre pares live, aplicada por un humano con --promote.
        r["holdout_pass"] = bool(reject and r["mean_margin_vs_champion"] >= MATERIAL_MARGIN)
        r["promotable"] = r["holdout_pass"]  # alias deprecado (dual-read; retirar tras migrar consumidores)

    promotable = [r for r in rows if r["holdout_pass"]]
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


#: Recibo del CRPS de la receta campeona (`run_champion_crps.py`). M74-F · deuda 1.
CRPS_RECEIPT_SCHEMA = "champion-crps-receipt/1"
#: Rejilla de cuantiles de la cuadratura del CRPS (la misma convención que `eval_deep_pi.py`).
CRPS_QUANTS = (0.025, 0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 0.975)
#: La corrida COMPLETA de `run_champion_crps` (sus valores por defecto): lo único que una complementaria
#: `champion_crps` acredita. Un ``--limit`` o un muestreo más corto son otro protocolo, no esta cifra.
CRPS_PROTOCOLO: dict[str, Any] = {
    "tables": ["FAD", "DFF"],
    "block": "family",
    "num_samples": 200,
    "quants": list(CRPS_QUANTS),
}

#: Recibo del CRPS de referencia de los modelos distribucionales (`run_crps_baseline.py`). M74-F · C6.
CRPS_BASE_RECEIPT_SCHEMA = "baseline-crps-receipt/1"
#: La corrida COMPLETA de `run_crps_baseline` que una complementaria `baseline_crps` acredita: `crps_fad.csv`
#: (el insumo del panel CRPS de F4), todos los modelos distribucionales, su muestreo y la semilla con la que
#: las fábricas siembran ARIMA/SARIMA (y las redes): sin ella el CRPS no se repite.
CRPS_BASE_PROTOCOLO: dict[str, Any] = {
    "table": "FAD",
    "block": "family",
    "models": sorted(PROBABILISTIC),
    "num_samples": 200,
    "seed": RANDOM_SEED,
}


def crps_champion(table: str, path: Path | None = None) -> float | None:
    """Mean champion CRPS for a table, if ``reports/eval/crps_champion.csv`` exists (AM5).

    INFORMATIVE field of the verdict only — NOT part of the promotion gate (the gate stays
    point-MASE + Wilcoxon/Holm). Degrades cleanly to None when the CSV is absent or its
    schema is unexpected (it is produced by a separate CRPS pipeline).

    ⚠️ M74-F · este lector devuelve el VALOR CRUDO, sin decir de qué añada es. Quien lo publique
    tiene que pasar por :func:`crps_provenance`, que es el que acredita.
    """
    path = path if path is not None else REPORTS / "eval" / "crps_champion.csv"
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path)
        if "table" in df.columns:
            df = df[df["table"] == table]
        col = next((c for c in df.columns if "crps" in c.lower()), None)
        if col is None or df.empty:
            return None
        # run_champion_crps.py appends a country=="ALL" aggregate row per table — prefer it
        # over re-averaging the per-series rows (which would double-count the aggregate).
        if "country" in df.columns and (df["country"] == "ALL").any():
            df = df[df["country"] == "ALL"]
        val = float(df[col].mean())
        return round(val, 4) if val == val else None
    except Exception:  # noqa: BLE001 — informative only; never break the verdict
        return None


#: Estados de la campaña madre en los que su recibo de CRPS describe algo que esa campaña produjo.
CRPS_CAMPANA_ACREDITA = frozenset({"running", "computed", "validated"})


def _sin_lanes(*_a: object, **_k: object) -> None:
    raise RuntimeError("una complementaria de CRPS no tiene lanes que acreditar")


def _transaccion_del_crps(
    acta: dict, root: Path, parent_root: Path | None, git, proposito: str = "champion_crps"
) -> tuple[str | None, dict | None]:
    """``(motivo, None)`` si ninguna transacción de ``root`` acredita el recibo; ``(None, transacción)`` si sí.

    #66: el ``campaign_id`` del recibo era una cadena que nadie resolvía. Aquí se resuelve contra la
    campaña o la complementaria del MISMO árbol, y la complementaria se re-acredita con su lector. G7: o
    contra la reconciliación del árbol (`vp_model.reconciliation`), que trae la complementaria como fuente.
    """
    from vp_model import artifact_receipt as ar
    from vp_model import complement as cx

    cid = acta.get("campaign_id")
    campana = root / cx.CAMPANA_REL
    if campana.is_file():
        try:
            estado = ar.loads_strict(campana.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            return f"transacción de campaña ilegible: {exc}", None
        from vp_model.campaign_schema import validate_schema

        fuera = validate_schema(estado)  # G7 · puerta 1: una transacción de juguete no presta identidad
        if fuera:
            return f"la transacción de campaña no cumple el esquema de campaign.json: {fuera[0]}", None
        status = estado.get("status")
        if estado.get("campaign_id") != cid:
            return f"el recibo es de {cid!r} y la campaña de este árbol es {estado.get('campaign_id')!r}", None
        if status not in CRPS_CAMPANA_ACREDITA:
            return f"la campaña {cid!r} está en {status!r}: una campaña que no produjo no acredita", None
        if (estado.get("source_git_sha"), estado.get("panel_sha256")) != (
            acta.get("code_sha"),
            acta.get("panel_sha256"),
        ):
            return f"el recibo dice ser de {cid!r} con otro código o panel que esa campaña", None
        return None, {"kind": "campaign", "status": status}
    if (root / cx.RUTA_REL).is_file():
        try:
            doc = cx.leer(root)
        except cx.ComplementoError as exc:
            return f"complementaria ilegible: {exc}", None
        if doc["complement_id"] != cid:
            return f"el recibo es de {cid!r} y la complementaria de este árbol es {doc['complement_id']!r}", None
        if doc["purpose"] != proposito:
            return f"la complementaria {cid!r} es de {doc['purpose']!r}, no de {proposito}", None
        if doc["status"] != "accredited":
            return f"la complementaria {cid!r} está en {doc['status']!r}; sólo una 'accredited' acredita", None
        if parent_root is None:
            return f"la complementaria {cid!r} sólo se re-acredita con la raíz de su madre (parent_root)", None
        try:
            cx.acreditar(root, parent_root=Path(parent_root), acreditar_lanes=_sin_lanes, git=git or cx.estado_git)
        except cx.ComplementoError as exc:
            return f"el lector no acredita la complementaria {cid!r}: {exc}", None
        return None, {"kind": "complement", "status": "accredited"}
    from vp_model import reconciliation as rc

    if (root / rc.RUTA_REL).is_file():
        return _reconciliacion_del_crps(cid, root, parent_root, git, proposito)
    return f"el recibo nombra {cid!r} y ninguna transacción de este árbol lo acredita", None


def _reconciliacion_del_crps(
    cid: object, root: Path, parent_root: Path | None, git, proposito: str
) -> tuple[str | None, dict | None]:
    """G7: el recibo es de una FUENTE de la reconciliación del árbol, de ese propósito, y la reconciliación
    —con esa fuente re-acreditada en su propio árbol— se acredita entera al leer."""
    from vp_model import complement as cx
    from vp_model import reconciliation as rc

    try:
        doc = rc.leer(root)
    except rc.ReconciliacionError as exc:
        return f"reconciliación ilegible: {exc}", None
    fuente = next((s for s in doc["sources"] if s["complement_id"] == cid), None)
    if fuente is None:
        return f"el recibo es de {cid!r} y ninguna fuente de la reconciliación de este árbol lo es", None
    if fuente["purpose"] != proposito or cx.SALIDA_CRPS[proposito] not in fuente["artifacts"]:
        return f"la fuente {cid!r} es de {fuente['purpose']!r}, no responde por un CRPS {proposito}", None
    if parent_root is None:
        return "la reconciliación sólo se re-acredita con la raíz de la madre (parent_root)", None
    try:
        acta = rc.acreditar(root, parent_root=Path(parent_root), solo=str(cid), git=git or cx.estado_git)
    except rc.ReconciliacionError as exc:
        return f"el lector no acredita la reconciliación {doc['reconciliation_id']!r}: {exc}", None
    return None, {"kind": "reconciliation", "status": acta["status"], "reconciliation_id": acta["reconciliation_id"],
                  "source": cid}  # fmt: skip


def _recibo_acreditado(
    path: Path,
    schema: str,
    proposito: str,
    *,
    root: Path,
    panel: Path,
    parent_root: Path | None,
    git,
    validar,
) -> tuple[str | None, dict, dict | None]:
    """``(motivo, acta, transacción)`` del recibo de ``path``: motivo ``None`` sólo si lo acredita una transacción.

    Recibo legible del esquema pedido, sha del CSV, panel vigente en disco y una transacción del MISMO árbol
    que lo produjo (`_transaccion_del_crps`, con el propósito de complementaria que responde por ese CSV).
    Auditoría C6: la transacción sola no basta; ``validar`` (protocolo, forma y sustancia, sin identidad) corre
    también cuando la respalda una CAMPAÑA, que antes sólo se comprobaba por identidad. Devuelve problemas,
    nunca lanza.
    """
    from vp_model import artifact_receipt as ar

    recibo = ar.receipt_path(path)
    if not recibo.is_file():
        return "sin recibo: el CSV no dice de qué campaña ni de qué panel es", {}, None
    try:
        acta = ar.loads_strict(recibo.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        return f"recibo ilegible: {exc}", {}, None
    if acta.get("schema") != schema:
        return f"recibo con esquema {acta.get('schema')!r}", acta, None
    if acta.get("artifact_sha256") != ar.sha256_file(path):
        return "el CSV cambió desde que se selló su recibo", acta, None
    if not panel.is_file():
        return f"no hay panel en disco ({panel}) contra el que acreditar", acta, None
    if acta.get("panel_sha256") != "sha256:" + ar.sha256_file(panel):
        return "el recibo es de otro panel: el CRPS no describe los datos vigentes", acta, None
    motivo, transaccion = _transaccion_del_crps(acta, root, parent_root, git, proposito)
    if motivo is not None:
        return motivo, acta, None
    problemas = validar(root, acta)
    if problemas:
        return f"el recibo no supera la validación: {sorted({c for c, _ in problemas})} · {problemas[0][1]}", acta, None
    return None, acta, transaccion


def crps_provenance(
    table: str,
    *,
    reports: Path | None = None,
    panel: Path | None = None,
    parent_root: Path | None = None,
    git=None,
) -> dict:
    """CRPS campeón de ``table`` **con su procedencia acreditada al leer** (M74-F · deuda 1).

    La cuarta campaña real dejó `crps_champion.csv` byte-idéntico al de julio y, aun así, su cifra
    llegó a ``key_facts`` y a ``\\factCrpsFad`` como si fuera de la campaña: `crps_champion` lee lo
    que haya en disco y nadie preguntaba de cuándo es. Aquí el valor sólo se entrega como vigente
    (``status="acreditado"``) si su recibo:

    * existe, tiene el esquema ``champion-crps-receipt/1`` y su ``artifact_sha256`` es el del CSV;
    * declara el ``panel_sha256`` del panel que hay HOY en disco — un CRPS de otra añada de datos no
      describe el panel vigente, aunque nadie haya tocado el archivo;
    * y su ``campaign_id`` lo resuelve una transacción del MISMO árbol (#66): la campaña de ese id en
      ``running``/``computed``/``validated`` con el código y el panel del recibo, o la complementaria
      ``champion_crps`` de ese id ``accredited`` que su lector (`complement.acreditar`) vuelve a
      acreditar entera con la raíz de su madre (``parent_root``); o, G7, una FUENTE de ese id y propósito de
      la reconciliación del árbol (`vp_model.reconciliation`), que se acredita entera re-acreditando esa
      fuente en su propio árbol. Un id que nadie resuelve, una transacción ``failed`` o una complementaria
      sólo ``computed`` no acreditan.

    Cualquier otra cosa es ``heredado`` (el CSV existe pero no se acredita: su valor se conserva en
    ``inherited_value`` con su sha256, identificado como lo que es) o ``ausente``. Nunca lanza:
    el CRPS es informativo y no puede tumbar el veredicto.
    """
    from vp_model import artifact_receipt as ar

    reports = Path(reports) if reports is not None else REPORTS
    panel = Path(panel) if panel is not None else ROOT / ar.PANEL_REL
    path = reports / "eval" / "crps_champion.csv"
    if not path.is_file():
        return {
            "status": "ausente",
            "value": None,
            "inherited_value": None,
            "artifact_sha256": None,
            "reason": f"no existe {path.name}",
        }
    crudo = crps_champion(table, path)
    base = {"value": None, "inherited_value": crudo, "artifact_sha256": ar.sha256_file(path)}

    def heredado(motivo: str) -> dict:
        return {"status": "heredado", **base, "reason": motivo}

    from vp_model import complement as cx

    motivo, acta, transaccion = _recibo_acreditado(
        path,
        CRPS_RECEIPT_SCHEMA,
        "champion_crps",
        root=reports.parent,
        panel=panel,
        parent_root=parent_root,
        git=git,
        validar=cx.validar_crps,
    )
    if motivo is not None:
        return heredado(motivo)
    if crudo is None:
        return {"status": "ausente", **base, "reason": f"el CSV acreditado no trae CRPS para {table}"}
    return {
        "status": "acreditado",
        **base,
        "value": crudo,
        "inherited_value": None,
        "receipt_campaign_id": acta.get("campaign_id"),
        "transaction": transaccion,
        "reason": None,
    }


def _medias_por_modelo(path: Path) -> dict[str, float | None]:
    """Media del CRPS por modelo de `crps_fad.csv`; las omisiones (celda vacía) no entran en la media."""
    df = pd.read_csv(path)
    medias = df.groupby("model")["crps"].mean()
    return {str(m): (round(float(v), 4) if v == v else None) for m, v in medias.items()}


def baseline_crps_provenance(
    *,
    reports: Path | None = None,
    panel: Path | None = None,
    parent_root: Path | None = None,
    git=None,
) -> dict:
    """Medias del CRPS de referencia (`crps_fad.csv`) **con su procedencia acreditada al leer** (M74-F · C6).

    Lo que consume el panel CRPS de F4. Las mismas reglas que :func:`crps_provenance`, con el recibo
    ``baseline-crps-receipt/1`` y la complementaria de propósito ``baseline_crps``: sólo ``acreditado`` entrega
    ``means`` vigentes; ``heredado`` conserva las medias en ``inherited_means`` identificadas por su sha256.
    Nunca lanza.
    """
    from vp_model import artifact_receipt as ar

    reports = Path(reports) if reports is not None else REPORTS
    panel = Path(panel) if panel is not None else ROOT / ar.PANEL_REL
    path = reports / "eval" / f"crps_{str(CRPS_BASE_PROTOCOLO['table']).lower()}.csv"
    if not path.is_file():
        return {"status": "ausente", "means": None, "inherited_means": None, "artifact_sha256": None,
                "reason": f"no existe {path.name}"}  # fmt: skip
    try:
        medias: dict[str, float | None] | None = _medias_por_modelo(path)
    except ValueError, KeyError, OSError:
        medias = None
    base = {"means": None, "inherited_means": medias, "artifact_sha256": ar.sha256_file(path)}
    from vp_model import complement as cx

    motivo, acta, transaccion = _recibo_acreditado(
        path,
        CRPS_BASE_RECEIPT_SCHEMA,
        "baseline_crps",
        root=reports.parent,
        panel=panel,
        parent_root=parent_root,
        git=git,
        validar=cx.validar_crps_base,
    )
    if motivo is not None:
        return {"status": "heredado", **base, "reason": motivo}
    if medias is None:
        return {"status": "ausente", **base, "reason": f"el CSV acreditado no se puede leer como {path.name}"}
    return {"status": "acreditado", **base, "means": medias, "inherited_means": None,
            "receipt_campaign_id": acta.get("campaign_id"), "transaction": transaccion, "reason": None}  # fmt: skip


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
