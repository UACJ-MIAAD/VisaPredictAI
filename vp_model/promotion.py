"""Gate de promoción PROSPECTIVO pre-registrado (A4, plan auditoría 2026-07-11).

**Pre-registro:** esta política se fijó el 2026-07-11 con CERO pares live puntuados
(``prospective_head_to_head.json``: n_pairs=0), antes de ver cualquier resultado que
pudiera sesgarla. Cambiarla exige editar ``POLICY`` (versionado en git) ANTES de la
añada que se pretenda juzgar, nunca después de ver sus resultados.

**Separación de vocabulario (A4):** el veredicto retrospectivo del hold-out
(``champion_challenger.json``, campo ``holdout_pass``; antes ``promotable``) declara
aptitud en h=1 retrospectivo y NO autoriza producción. La autorización la da ESTE gate
sobre pares live campeón-vs-sombra (protocolo P6 de ``docs/FORECAST_EVAL.md``) y la
aplica un humano con ``run_champion_challenger.py --promote`` (que se rehúsa sin una
decisión ``promote`` vigente). **Rollback pre-registrado:** ``champion_manifest.json``
está versionado en git — revertir el commit y redesplegar el demostrador.

**Autocorrelación:** los pronósticos de orígenes consecutivos se traslapan; la unidad
de inferencia NO es la fila sino la **serie** (mediana de ``scaled_err`` por serie y
banda de horizonte), con las réplicas exactas del corte mundial colapsadas a una y
Holm entre bandas dentro de cada tabla.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

POLICY: dict = {
    # 1.0.1: fix de implementación a la política registrada (11-jul, auditoría del autor,
    # aún con 0 pares live vistos): Holm aplicado también a la familia del RECHAZO —
    # el texto registrado decía "significativamente peor" bajo Holm; el código usaba
    # p_worse crudo. Ningún parámetro cambió.
    "policy_version": "1.0.1",
    "preregistered_at": "2026-07-11",
    "preregistered_with_zero_live_pairs": True,
    # Solo P6 (añadas servidas en vivo) autoriza — el backfill jamás produce promoción.
    "modes_allowed": ["live"],
    # Bandas de horizonte: el producto sirve h=1..12; el gate exige evidencia en TODAS.
    "horizon_bands": {"h1_3": [1, 3], "h4_6": [4, 6], "h7_12": [7, 12]},
    # Muestra mínima: pares por tabla y banda, y añadas live distintas con pares.
    "min_pairs_per_band": 30,
    "min_live_vintages": 3,
    # Margen material: mejora relativa de MASE por serie requerida en cada banda.
    "material_margin": 0.10,
    # Retroceso máximo tolerado en una banda antes de rechazar al retador.
    "max_band_regression": 0.05,
    # Cobertura del intervalo 95 % del retador sobre pares live.
    "min_cov95": 0.90,
    "max_cov95_gap_vs_champion": 0.03,
    # Wilcoxon pareado unilateral por serie + Holm entre bandas (por tabla).
    "alpha_holm": 0.05,
    "unit": "serie (mediana de scaled_err por serie y banda; réplicas exactas del corte mundial colapsan a una)",
    "hypotheses": {
        "h1": "H_A: el retador no es inferior al campeón a h=1 en pares live",
        "h1_12": "H_A: el retador mejora materialmente al campeón en TODAS las bandas h=1..12 en pares live",
    },
}

DECISIONS = ("promote", "retain", "extend-shadow", "reject")


def band_of(h: int, bands: dict[str, list[int]]) -> str | None:
    for name, (lo, hi) in bands.items():
        if lo <= h <= hi:
            return name
    return None


def _dedup_pairs(pairs: pd.DataFrame) -> pd.DataFrame:
    """Colapsa réplicas exactas del corte mundial (misma convención que el hold-out B2).

    Cuando el boletín publica el corte mundial, la fila del país es idéntica a la de
    ``all_chargeability``: mismos targets, mismos actuals y mismas predicciones en ambos
    lados. Contarlas infla n sin información nueva. Se conserva la serie mundial y se
    descarta la réplica exacta del país.
    """
    if not len(pairs):
        return pairs
    sig_cols = ["target", "actual_champ", "pred_champ", "pred_shadow"]
    have = [c for c in sig_cols if c in pairs.columns]

    def _sig(g: pd.DataFrame) -> tuple:
        return tuple(map(tuple, g.sort_values("target")[have].to_numpy().tolist()))

    keep: list[pd.DataFrame] = []
    for (_cat, _table), g in pairs.groupby(["category", "table"]):
        world = g[g["country"] == "all_chargeability"]
        wsig = {_sig(gg) for _, gg in world.groupby("country")} if len(world) else set()
        for country, gg in g.groupby("country"):
            if country != "all_chargeability" and _sig(gg) in wsig:
                continue  # réplica exacta del corte mundial — colapsa
            keep.append(gg)
    return pd.concat(keep, ignore_index=True) if keep else pairs.iloc[0:0]


def _series_band_medians(pairs: pd.DataFrame, bands: dict[str, list[int]]) -> pd.DataFrame:
    p = pairs.copy()
    p["band"] = p["h"].map(lambda h: band_of(int(h), bands))
    p = p[p["band"].notna()]
    return (
        p.groupby(["table", "band", "country", "category"])[["scaled_err_champ", "scaled_err_shadow"]]
        .median()
        .reset_index()
    )


def decide(pairs: pd.DataFrame, policy: dict = POLICY) -> dict:
    """Decisión pre-registrada por tabla: promote · retain · extend-shadow · reject.

    ``pairs`` = merge de los scorecards campeón y sombra por (origin, serie, target, h)
    con sufijos ``_champ``/``_shadow`` y ``evaluation_mode`` coincidente en ambos lados
    (la salida de ``score_forecasts._pairs``). La muestra insuficiente NUNCA promueve:
    degrada a ``extend-shadow`` con la razón explícita.
    """
    out: dict = {"policy": policy, "by_table": {}}
    live = (
        pairs[
            (pairs["evaluation_mode_champ"].isin(policy["modes_allowed"]))
            & (pairs["evaluation_mode_champ"] == pairs["evaluation_mode_shadow"])
        ]
        if len(pairs)
        else pairs
    )
    out["n_pairs_total"] = int(len(pairs))
    out["n_pairs_live"] = int(len(live))
    tables = sorted(pairs["table"].unique()) if len(pairs) else []
    if not tables:
        out["note"] = "sin pares campeón-sombra aún — el gate espera añadas live puntuadas"
    for table in tables:
        out["by_table"][str(table)] = _decide_table(live[live["table"] == table], policy)
    return out


def _decide_table(tl: pd.DataFrame, policy: dict) -> dict:
    reasons: list[str] = []
    bands = policy["horizon_bands"]
    res: dict = {"decision": "extend-shadow", "reasons": reasons, "n_pairs_live": int(len(tl))}
    if not len(tl):
        reasons.append("0 pares live — muestra insuficiente; seguir acumulando sombra")
        return res
    n_vint = int(tl["origin"].nunique())
    res["n_live_vintages"] = n_vint
    if n_vint < policy["min_live_vintages"]:
        reasons.append(f"añadas live con pares: {n_vint} < mínimo {policy['min_live_vintages']}")
        return res
    tl = _dedup_pairs(tl)
    per_band_n = {b: int((tl["h"].map(lambda h: band_of(int(h), bands)) == b).sum()) for b in bands}
    res["n_pairs_by_band"] = per_band_n
    short = [b for b, n in per_band_n.items() if n < policy["min_pairs_per_band"]]
    if short:
        reasons.append(f"bandas con muestra insuficiente (<{policy['min_pairs_per_band']} pares): {short}")
        return res

    med = _series_band_medians(tl, bands)
    from scipy.stats import wilcoxon  # lazy: el env dev de CI no trae scipy

    from vp_model import significance

    stats: dict[str, dict] = {}
    pvals: dict[str, float] = {}
    for b, g in med.groupby("band"):
        ch, sh = g["scaled_err_champ"], g["scaled_err_shadow"]
        margin = float(1.0 - sh.mean() / ch.mean()) if ch.mean() else 0.0
        better_p = float(wilcoxon(ch, sh, alternative="greater").pvalue) if len(g) >= 5 else 1.0
        worse_p = float(wilcoxon(ch, sh, alternative="less").pvalue) if len(g) >= 5 else 1.0
        stats[str(b)] = {
            "n_series": int(len(g)),
            "rel_margin": round(margin, 4),
            "p_better": round(better_p, 5),
            "p_worse": round(worse_p, 5),
        }
        pvals[str(b)] = better_p
    adj = significance.holm(pvals, alpha=policy["alpha_holm"])
    for b, (p_adj, reject) in adj.items():
        stats[b]["holm_p_better"] = round(float(p_adj), 5)
        stats[b]["significantly_better"] = bool(reject)
    # Auditoría 11-jul: la familia del RECHAZO también se prueba en múltiples bandas —
    # sin Holm, un falso positivo familiar podía rechazar (la política documenta Holm
    # para AMBAS direcciones; el promote ya lo aplicaba).
    adj_worse = significance.holm({b: s["p_worse"] for b, s in stats.items()}, alpha=policy["alpha_holm"])
    for b, (p_adj, reject) in adj_worse.items():
        stats[b]["holm_p_worse"] = round(float(p_adj), 5)
        stats[b]["significantly_worse"] = bool(reject)
    res["by_band"] = stats

    cov_sh = float(tl["in95_shadow"].mean())
    cov_ch = float(tl["in95_champ"].mean())
    res["cov95"] = {"shadow": round(cov_sh, 3), "champion": round(cov_ch, 3)}
    cov_ok = cov_sh >= policy["min_cov95"] and cov_sh >= cov_ch - policy["max_cov95_gap_vs_champion"]

    worse = [
        b for b, s in stats.items() if s["rel_margin"] <= -policy["max_band_regression"] and s["significantly_worse"]
    ]
    if worse:
        res["decision"] = "reject"
        reasons.append(
            f"el retador es significativamente PEOR en {worse} (retroceso > {policy['max_band_regression']:.0%})"
        )
        return res
    all_good = all(
        s["rel_margin"] >= policy["material_margin"] and s.get("significantly_better") for s in stats.values()
    )
    if all_good and cov_ok:
        res["decision"] = "promote"
        reasons.append("mejora material y Holm-significativa en TODAS las bandas, con cobertura 95 % en regla")
        reasons.append(
            "requiere aprobación humana: run_champion_challenger.py --promote (rollback: git revert del manifiesto)"
        )
        return res
    res["decision"] = "retain"
    if not cov_ok:
        reasons.append(f"cobertura 95 % del retador fuera de política ({cov_sh:.3f})")
    if not all_good:
        reasons.append(
            "mejora no material o no significativa en al menos una banda — mantener campeón y seguir midiendo"
        )
    return res


def candidate_hash(candidate: dict, policy: dict) -> str:
    """sha256-12 canónico de la identidad COMPLETA del candidato (R0-01): política íntegra
    + campos del candidato (sin el propio hash) — la decisión deja de ser campos
    autorreportados sueltos; mutar cualquiera invalida el hash."""
    import hashlib

    core = {k: v for k, v in candidate.items() if k != "hash"}
    blob = json.dumps({"policy": policy, "candidate": core}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def evidence_hashes(vintages: list[str] | None = None, root: Path | None = None) -> dict[str, str]:
    """sha256-12 de la evidencia que sustenta la decisión (R0-01 + reauditoría 3): los dos
    scorecards y el ledger sombra, FILTRADOS a las filas cuyas añadas (``origin``) son las
    de la decisión y serializados canónicamente.

    Reauditoría 3 (P1): el hash de ARCHIVO COMPLETO se auto-invalidaba — el propio cron
    apendea la añada sombra nueva (freeze_shadow) DESPUÉS del gate, y el scoring regenera
    los scorecards cada mes. El crecimiento append-only legítimo NO debe matar la decisión;
    una REESCRITURA de las filas que son su evidencia, sí. Filtrar por las añadas de la
    decisión logra exactamente eso.
    """
    import hashlib

    base = root or Path(__file__).resolve().parent.parent
    wanted = {str(v) for v in vintages} if vintages else None
    out: dict[str, str] = {}
    for label, rel in (
        ("scorecard_champion", "reports/prospective/forecast_scorecard.csv"),
        ("scorecard_shadow", "reports/prospective/forecast_scorecard_shadow.csv"),
        ("shadow_ledger", "reports/prospective/forecast_log_shadow.csv"),
    ):
        p = base / rel
        if not p.exists():
            out[label] = "n/d"
            continue
        try:
            df = pd.read_csv(p)
        except pd.errors.EmptyDataError:
            out[label] = "vacio"
            continue
        if wanted is not None and "origin" in df.columns:
            df = df[df["origin"].astype(str).isin(wanted)]
        # Reauditoría 4: canónico también en COLUMNAS — permutar columnas del mismo
        # contenido no debe cambiar el hash.
        df = df[sorted(df.columns)]
        blob = df.sort_values(list(df.columns), na_position="last").to_csv(index=False)
        out[label] = hashlib.sha256(blob.encode()).hexdigest()[:12]
    return out


def shadow_evidence(table: str, vintages: list[str]) -> tuple[set[str], set[str]]:
    """(recetas, añadas) REALES del ledger sombra para esta tabla y estas añadas —
    la fuente contra la que el candidato debe coincidir EXACTAMENTE (reauditoría 5: la
    unión de orígenes autorizaba listas con recetas fantasma; el fallback por display
    name era fail-open). Se deriva del ledger, jamás de lo autorreportado."""
    p = Path(__file__).resolve().parent.parent / "reports" / "prospective" / "forecast_log_shadow.csv"
    if not p.exists():
        return set(), set()
    df = pd.read_csv(p, usecols=["origin", "table", "recipe"])
    mask = (df["table"].astype(str) == str(table)) & (df["origin"].astype(str).isin([str(v) for v in vintages]))
    hit = df[mask]
    return set(hit["recipe"].astype(str).unique()), set(hit["origin"].astype(str).unique())


def _candidate_violations(cand: dict, policy: dict, table: str) -> list[str]:
    """Validez intrínseca del candidato (R0-01): vintages plausibles y con muestra
    mínima, fecha de decisión parseable y no futura, hash íntegro."""
    import datetime
    import re

    v: list[str] = []
    vintages = cand.get("vintages")
    if not isinstance(vintages, list) or not vintages:
        v.append("candidato sin añadas live (vintages vacío/ausente)")
    else:
        from vp_model import ledger

        panel_now = ledger.panel_vintage()
        bad = [x for x in vintages if not re.fullmatch(r"\d{4}-\d{2}", str(x))]
        future = [x for x in vintages if str(x) > panel_now]
        if bad:
            v.append(f"añadas con formato inválido: {bad}")
        if future:
            v.append(f"añadas POSTERIORES al panel vigente ({panel_now}): {future}")
        # Reauditoría 2: tres copias de la MISMA añada satisfacían el piso — el conteo es
        # sobre añadas ÚNICAS, y cada una debe EXISTIR en el ledger sombra (evidencia).
        if len(set(vintages)) != len(vintages):
            v.append(f"añadas DUPLICADAS en la decisión: {sorted(vintages)}")
        if len(set(vintages)) < policy["min_live_vintages"]:
            v.append(f"añadas únicas declaradas: {len(set(vintages))} < mínimo {policy['min_live_vintages']}")
        # Reauditoría 5: challenger_recipes es OBLIGATORIA (el fallback por display name
        # era fail-open) y debe coincidir EXACTAMENTE con las recetas del ledger para
        # (tabla, añadas) — ni fantasmas ni faltantes; la unión de orígenes no basta.
        recipes = cand.get("challenger_recipes")
        if (
            not isinstance(recipes, list)
            or not recipes
            or not all(isinstance(r, str) and r for r in recipes)
            or len(set(recipes)) != len(recipes)
        ):
            v.append(
                "challenger_recipes ausente/vacío/no-strings/duplicado — la lista EXACTA es obligatoria (fail closed)"
            )
        else:
            real_recipes, real_origins = shadow_evidence(table, [str(x) for x in vintages])
            declared = set(recipes)
            if declared != real_recipes:
                v.append(
                    f"recetas declaradas {sorted(declared)} ≠ recetas REALES del ledger sombra para "
                    f"{table}/añadas {sorted(real_recipes)} (ni fantasmas ni faltantes)"
                )
            elif len(declared) > 1:
                v.append(
                    f"la evidencia mezcla {len(declared)} recetas sombra {sorted(declared)} — no autoriza "
                    "la promoción de una receta individual (fail closed; homogeneizar la ventana y re-correr)"
                )
            elif str(cand.get("challenger")) != next(iter(declared)):
                v.append(
                    f"challenger (display {cand.get('challenger')!r}) no corresponde a la receta única "
                    f"autorizada ({next(iter(declared))!r})"
                )
            ghosts = sorted(set(str(x) for x in vintages) - real_origins)
            if ghosts:
                v.append(f"añadas declaradas que NO existen en el ledger sombra para {table}: {ghosts}")
    da = cand.get("decided_at")
    try:
        when = datetime.datetime.fromisoformat(str(da))
        if when > datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=5):
            v.append(f"decided_at en el futuro: {da}")
    except ValueError, TypeError:
        v.append(f"decided_at no es una fecha ISO válida: {da!r}")
    if cand.get("hash") != candidate_hash(cand, policy):
        v.append("candidate_hash no re-deriva de sus campos (identidad manipulada o formato viejo)")
    stored_ev = cand.get("evidence")
    if not isinstance(stored_ev, dict) or not stored_ev:
        v.append("candidato sin hashes de evidencia (formato pre-R0-01)")
    else:
        now_ev = evidence_hashes(vintages=cand.get("vintages") or [])
        drift = {k: (stored_ev.get(k), now_ev.get(k)) for k in now_ev if stored_ev.get(k) != now_ev.get(k)}
        if drift:
            v.append(f"la evidencia en disco YA NO es la de la decisión: {drift}")
    return v


def authorize(table: str, decision_path: Path, *, challenger: str, champion: str) -> tuple[bool, str]:
    """¿La decisión prospectiva vigente autoriza promover ESTE candidato? Fail closed.

    A-02 (auditoría ciega 11-jul): una decisión ``promote`` NO es un cheque al portador.
    La autorización se liga a la identidad COMPLETA evaluada: política íntegra (no solo
    la versión), release vigente al decidir, campeón y retador exactos. Cualquier
    diferencia — política editada, release nuevo, otro retador, campeón cambiado o una
    decisión de formato viejo sin candidato — invalida la decisión y exige re-correr el
    gate. La reproducción del auditor (política ``0.0-stale`` + retador ajeno) muere aquí.
    """
    if not decision_path.exists():
        return False, f"sin {decision_path.name} — corre experiments/run_promotion_gate.py primero (fail closed)"
    data = json.loads(decision_path.read_text())
    stored_policy = data.get("policy", {})
    if json.dumps(stored_policy, sort_keys=True) != json.dumps(POLICY, sort_keys=True):
        return False, (
            f"la política de la decisión (v{stored_policy.get('policy_version')}) NO es la política "
            f"vigente (v{POLICY['policy_version']}) — decisión inválida; re-corre el gate (fail closed)"
        )
    entry = data.get("by_table", {}).get(table)
    if not entry:
        return False, f"la decisión vigente no cubre la tabla {table} (fail closed)"
    if entry.get("decision") != "promote":
        return False, f"decisión prospectiva vigente = {entry.get('decision')!r}: {'; '.join(entry.get('reasons', []))}"
    cand = entry.get("candidate")
    if not cand:
        return False, "decisión sin identidad de candidato (formato pre-A-02) — re-corre el gate (fail closed)"
    # Reauditoría 2 (12-jul): la igualdad release==vigente se AUTO-INVALIDABA — la propia
    # promotion_decision.json entra al manifiesto, así que el sellado posterior al gate
    # cambiaba el release y mataba toda promoción humana normal. La liga SUSTANTIVA con
    # el corte son los hashes de EVIDENCIA (scorecards + ledger sombra, recomputados del
    # disco): si un boletín nuevo llegó, la evidencia cambió y la decisión muere por esa
    # regla. release_id queda REGISTRADO como procedencia, no como candado.
    if cand.get("challenger") != challenger:
        return False, (
            f"el retador a promover ({challenger!r}) NO es el que ganó la evidencia prospectiva "
            f"({cand.get('challenger')!r}) — la decisión no lo autoriza (fail closed)"
        )
    if cand.get("champion") != champion:
        return False, (
            f"el campeón actual ({champion!r}) ya no es el evaluado por el gate "
            f"({cand.get('champion')!r}) — evidencia caduca; re-corre el gate (fail closed)"
        )
    intrinsic = _candidate_violations(cand, POLICY, table)
    if intrinsic:
        return False, "candidato inválido (fail closed): " + "; ".join(intrinsic)
    return True, (
        f"decisión prospectiva = promote para {challenger!r} vs {champion!r} — evidencia sellada "
        f"bajo release {cand.get('release_id')} (política pre-registrada v{POLICY['policy_version']})"
    )
