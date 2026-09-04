"""Evaluación PROSPECTIVA (en tiempo real) de los pronósticos congelados.

El MASE del entregable es *retrospectivo* (hold-out: el modelo "predice" meses ya
conocidos). Esto es lo contrario: toma cada pronóstico **congelado** en
``reports/prospective/forecast_log.csv`` (lo que predijimos y desde qué mes — la "añada") y lo
compara con el **corte realmente publicado** después, conforme llegan los boletines.
Es la única medida honesta de qué tan bueno es el pronóstico a 12 meses en el mundo real.

Por cada fila del ledger cuyo mes-objetivo ya tiene un corte real (estado F en el panel):
  • error = predicho − real (días);   |error|;   error escalado (MASE) por la escala
    naïve estacional in-sample hasta el origen (leakage-free, misma def. que el .tex);
  • cobertura: ¿el real cayó dentro de la banda 80 % / 95 %?

Agrega global, por horizonte h=1..12 y por tabla. **A3 (plan auditoría 2026-07-11):** el
ledger SOMBRA se puntúa con la MISMA maquinaria (máscara F, escala, universo) a archivos
propios, los agregados JAMÁS combinan ``evaluation_mode`` backfill y live (``overall``/
``by_horizon``/``by_table`` quedan anclados al modo backfill; ``by_mode`` reporta cada
modo por separado) y se emite la comparación campeón-vs-sombra por pares del mismo
universo, consumible por el gate de promoción (A4). Salidas:
  • reports/prospective/forecast_scorecard.csv         — una fila por predicción campeón evaluable
  • reports/prospective/forecast_scorecard_meta.json   — agregados (MAE/MASE/cobertura, n, by_mode)
  • reports/prospective/forecast_scorecard_shadow.csv  — ídem para el ledger sombra
  • reports/prospective/forecast_scorecard_shadow_meta.json
  • reports/prospective/prospective_head_to_head.json  — pares campeón/sombra (mismo modo)
Tracking (experimento "web_forecast_scoring") es para desarrollo local; el registro
DURABLE es el scorecard commiteado en git (en CI el staging es efímero, y no hay servicio
remoto: solo el JSONL de staging de ``vp_data.tracking``).

**D7 — instrumentación mensual.** Cada invocación de ``run`` emite **exactamente un**
record ``web_forecast_scoring`` vía ``vp_model.tracking.track_run`` (antes eran uno global
más uno por horizonte, así que el número de records dependía del mes). El record se emite
también cuando no hay objetivos realizados, cuando falta el ledger y cuando el scoring
falla —en ese caso con ``status="failed"``, la excepción tipada y el error RE-LANZADO sin
alterar—, y lleva las métricas mínimas ``n_scored``/``n_pending``/``n_no_scale``/
``n_pairs``/``n_pairs_live`` más MAE/MASE/cobertura cuando existen. Además deja un
**marcador de resumen de UNA línea** en ``$VP_SCORING_SUMMARY`` (el cron pasa la ruta
explícita, como con sus otros marcadores) que pega en el correo como "Tracking mensual: ...";
sin marcador el correo dice ``n/d`` honestamente. La instrumentación observa: las salidas
de esta corrida son byte-idénticas a las de antes para las mismas entradas.

Al inicio de una añada nada está realizado aún → n=0 (correcto): la medición se
acumula mes a mes. Corre en ``ante``:  ante/bin/python experiments/score_forecasts.py
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
from pathlib import Path

import pandas as pd

from vp_model import config, dataset, intervals, metrics, promotion
from vp_model.tracking import TrackedRun, track_run

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
N_FLOOR = 30  # AN7: coverage blocks with n below this carry insufficient_n=true
log = config.get_logger("score_forecasts")

# D7: el marcador de una línea que el cron pega en el correo. Vive fuera de git (no es un
# artefacto versionado): es telemetría del run, no evidencia del release. La RUTA la fija
# quien invoca (el cron la pasa explícita, como con el resto de sus marcadores); el default
# solo cubre la corrida a mano.
SUMMARY_ENV = "VP_SCORING_SUMMARY"
DEFAULT_SUMMARY = Path(tempfile.gettempdir()) / "tracking_monthly.txt"
MIN_METRICS = ("n_scored", "n_pending", "n_no_scale", "n_pairs", "n_pairs_live")


def _score_rows(fc: pd.DataFrame, actuals: dict, scale_for) -> tuple[list[dict], int]:
    """Filas evaluables (objetivo ya realizado) + conteo de pendientes. Lógica pura,
    separada de la E/S para poder probarla con datos sintéticos (ver ``demo``)."""
    scored, pending = [], 0
    for r in fc.itertuples():
        actual = actuals.get((r.country, r.category, r.table, r.date))
        if actual is None:  # mes-objetivo aún no publicado, o no es estado F → no evaluable todavía
            pending += 1
            continue
        sc = scale_for(r.country, r.category, r.table, r.origin)
        abs_err = abs(r.days - actual)
        scored.append(
            {
                "origin": r.origin,
                "h": r.h,
                "country": r.country,
                "category": r.category,
                "table": r.table,
                "target": r.date,
                "pred": r.days,
                "actual": actual,
                "error": r.days - actual,
                "abs_err": abs_err,
                "scaled_err": abs_err / sc,
                "in80": int(r.lo80 <= actual <= r.hi80),
                "in95": int(r.lo95 <= actual <= r.hi95),
                # A3: el modo y la receta viajan del ledger v2 al scorecard para que los
                # agregados puedan separarse; frames pre-v2 (demo/tests) degradan a n/d.
                "evaluation_mode": getattr(r, "evaluation_mode", "n/d"),
                "model_version": getattr(r, "model_version", "n/d"),
            }
        )
    return scored, pending


def _agg(d: pd.DataFrame) -> dict:
    # AN7: every reported coverage carries a Jeffreys CI and its n; below the n floor
    # the block is flagged insufficient_n (a coverage on a handful of points is noise).
    n = int(len(d))
    out: dict[str, object] = {
        "n": n,
        "mae_days": round(float(d["abs_err"].mean()), 1),
        "mase": round(float(d["scaled_err"].mean()), 4),
        "cov80": round(float(d["in80"].mean()), 3),
        "cov95": round(float(d["in95"].mean()), 3),
    }
    for col in ("in80", "in95"):
        lo, hi = intervals.jeffreys_ci(int(d[col].sum()), n)
        out[f"cov{col[2:]}_ci95"] = [round(lo, 3), round(hi, 3)]
    if n < N_FLOOR:
        out["insufficient_n"] = True
    return out


def _mode_blocks(sdf: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """(filas backfill, bloques por modo) — A3: los agregados JAMÁS combinan modos.

    ``overall``/``by_horizon``/``by_table`` del meta se anclan al modo ``backfill``
    (hoy el único con filas puntuadas); cada modo reporta su propio bloque en
    ``by_mode`` y las añadas ``live`` se acumulan ahí sin diluirse ni diluir."""
    if not len(sdf) or "evaluation_mode" not in sdf.columns:
        return sdf, {}
    by_mode = {
        str(mode): {"overall": _agg(g), "by_horizon": {int(h): _agg(gh) for h, gh in g.groupby("h")}}
        for mode, g in sdf.groupby("evaluation_mode")
    }
    return sdf[sdf["evaluation_mode"] == "backfill"], by_mode


PAIR_KEYS = ["origin", "country", "category", "table", "target", "h"]


def _pairs(champ: pd.DataFrame, shadow: pd.DataFrame) -> pd.DataFrame:
    """Pares campeón/sombra por (añada, serie, target, h) con sufijos ``_champ``/``_shadow``.

    Única definición del pareo (A3/A4): la consumen ``_head_to_head`` y el gate de
    promoción (``experiments/run_promotion_gate.py``)."""
    if not len(champ) or not len(shadow):
        return pd.DataFrame()
    return champ.merge(shadow, on=PAIR_KEYS, suffixes=("_champ", "_shadow"))


def _head_to_head(champ: pd.DataFrame, shadow: pd.DataFrame) -> dict:
    """Comparación campeón-vs-sombra por pares del MISMO universo (A3 → gate A4).

    Un par = misma (añada, serie, fecha objetivo, h) puntuada en ambos ledgers. Solo se
    agregan pares cuyo ``evaluation_mode`` coincide en ambos lados (backfill con backfill,
    live con live); los pares de modo mixto se cuentan y se excluyen."""
    pair = _pairs(champ, shadow)
    if not len(pair):
        return {"n_pairs": 0, "n_mixed_mode_excluded": 0, "by_table": {}}
    out: dict[str, object] = {"n_pairs": int(len(pair)), "n_mixed_mode_excluded": 0, "by_table": {}}
    if not len(pair):
        return out
    same = pair[pair["evaluation_mode_champ"] == pair["evaluation_mode_shadow"]]
    out["n_mixed_mode_excluded"] = int(len(pair) - len(same))

    def _side(g: pd.DataFrame, suffix: str) -> dict:
        return {
            "mase": round(float(g[f"scaled_err{suffix}"].mean()), 4),
            "mae_days": round(float(g[f"abs_err{suffix}"].mean()), 1),
            "model_version": sorted(g[f"model_version{suffix}"].astype(str).unique()),
        }

    tables: dict[str, dict] = {}
    for (table, mode), g in same.groupby(["table", "evaluation_mode_champ"]):
        blk: dict[str, object] = {
            "n": int(len(g)),
            "champion": _side(g, "_champ"),
            "shadow": _side(g, "_shadow"),
            "by_horizon": {
                int(h): {
                    "n": int(len(gh)),
                    "champion_mase": round(float(gh["scaled_err_champ"].mean()), 4),
                    "shadow_mase": round(float(gh["scaled_err_shadow"].mean()), 4),
                }
                for h, gh in g.groupby("h")
            },
        }
        if len(g) < N_FLOOR:
            blk["insufficient_n"] = True
        tables.setdefault(str(table), {})[str(mode)] = blk
    out["by_table"] = tables
    return out


def _live_pairs(champ: pd.DataFrame, shadow: pd.DataFrame) -> int:
    """Pares campeón/sombra que el gate de promoción considera VIVOS (A4).

    Misma definición que ``vp_model.promotion.decide`` (``modes_allowed`` + modo idéntico
    en ambos lados) y el mismo pareo que ``_pairs``: se deriva, no se re-tipea. No toca
    ``prospective_head_to_head.json`` — es telemetría, no artefacto publicado."""
    pair = _pairs(champ, shadow)
    if not len(pair):
        return 0
    modes = promotion.POLICY["modes_allowed"]
    live = pair[
        pair["evaluation_mode_champ"].isin(modes) & (pair["evaluation_mode_champ"] == pair["evaluation_mode_shadow"])
    ]
    return int(len(live))


def _finite(values: dict) -> dict:
    """Solo métricas numéricas finitas: un MASE NaN (toda la añada sin escala) no es un cero."""
    out = {}
    for k, v in values.items():
        try:
            f = float(v)
        except TypeError, ValueError:
            continue
        if math.isfinite(f):
            out[k] = v
    return out


def _summary_path() -> Path:
    raw = os.environ.get(SUMMARY_ENV, "").strip()
    return Path(raw) if raw else DEFAULT_SUMMARY


def _one_line(text: object, limit: int = 120) -> str:
    """Colapsa a una sola línea y quita lo que rompería el JSON del correo."""
    return " ".join(str(text).split()).replace('"', "").replace("\\", "")[:limit]


def _wrote(tracked: TrackedRun, path: Path) -> None:
    """Registra una salida JUSTO DESPUÉS de escribirla.

    No se descubren artefactos con ``is_file()`` al final: un scorecard sombra de la añada
    anterior seguiría en disco cuando esta corrida no lo produce, y se registraría como si
    fuera suyo. Registrar al escribir también hace que un fallo a mitad deje anotadas —y
    solo— las salidas que sí llegaron a existir."""
    tracked.add_artifact(str(path))


def _summary_line(tracked: TrackedRun, error: BaseException | None) -> str:
    """Resumen de UNA línea, DERIVADO de las métricas y de la telemetría REAL del record.

    ``error`` es el resultado del comando, no el del bloque: si ``log_run`` falla tras un
    scoring correcto, ``telemetry["status"]`` dice ``ok`` pero el comando falló, y el
    marcador tiene que decir FALLO."""
    tel = tracked.telemetry
    status = "failed" if error is not None else str(tel.get("status", "n/d"))
    head = "ok" if error is None else f"FALLO {type(error).__name__}: {_one_line(error)}"
    parts = [head, f"status={status}"]
    parts += [f"{k}={int(tracked.metrics.get(k, 0))}" for k in MIN_METRICS]
    if "mase" in tracked.metrics:
        parts.append(f"MASE {float(tracked.metrics['mase']):.3f}")
    if "mae_days" in tracked.metrics:
        parts.append(f"MAE {float(tracked.metrics['mae_days']):.0f} d")
    if "cov95" in tracked.metrics:
        parts.append(f"cob95 {float(tracked.metrics['cov95']) * 100:.0f}%")
    dur = tel.get("duration_s")
    parts.append("duración n/d" if dur is None else f"{float(dur):.3f} s")
    rss = tel.get("rss_peak_mb")
    parts.append("RSS n/d" if rss is None else f"RSS {rss} MB")
    gpu = tel.get("gpu_mem_mb")
    parts.append("GPU n/a" if gpu is None else f"GPU {gpu} MB")
    art = tel.get("artifact_bytes")
    parts.append("artefactos n/d" if art is None else f"artefactos {int(art)} B")
    parts.append(f"warnings={len(tel.get('warnings', []))}")
    return _one_line(" · ".join(parts), limit=400)


def _write_summary(tracked: TrackedRun | None, error: BaseException | None) -> None:
    """Escribe el marcador SIN poder tapar nada.

    Corre en el ``finally`` de ``run``, así que cualquier excepción suya sustituiría al
    resultado real del scoring o del tracking: por eso el formateo entra también en el
    ``try``. Si no se puede escribir, queda el aviso en ``stderr`` y el correo dirá ``n/d``,
    que es la verdad."""
    if tracked is None:  # track_run murió antes de ceder el acumulador: nada que resumir
        return
    try:
        _summary_path().write_text(_summary_line(tracked, error) + "\n", encoding="utf-8")
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        print(f"[score_forecasts] marcador de resumen no escrito: {exc!r}", file=sys.stderr)


def run() -> Path | None:
    """Corrida mensual instrumentada: UN record por invocación + marcador de resumen.

    El marcador se escribe FUERA del context-manager a propósito: ``track_run`` intenta el
    registro al salir y RE-LANZA si ese registro falla, así que un marcador escrito dentro
    declararía ``ok`` en una corrida que el operador ve fallar."""
    tracked: TrackedRun | None = None
    error: BaseException | None = None
    result: Path | None = None
    try:
        with track_run(
            "web_forecast_scoring",
            "monthly",
            params={"scope": "prospective"},
            tags={"kind": "prospective_score"},
        ) as run_acc:
            tracked = run_acc
            result = _score_all(run_acc)
    except BaseException as exc:
        error = exc
        raise
    finally:
        _write_summary(tracked, error)
    return result


def _score_all(tracked: TrackedRun) -> Path | None:
    tracked.log_metrics(dict.fromkeys(MIN_METRICS, 0))
    log_path = REPORTS / "prospective" / "forecast_log.csv"
    if not log_path.exists():
        msg = f"no hay ledger {log_path} — corre generate_web_forecasts primero"
        log.warning("%s", msg)
        tracked.warn(msg)
        return None
    fc = pd.read_csv(log_path)
    actuals = dataset.actuals_F()

    # escala naïve in-sample hasta el origen, cacheada por (serie, origen) — leakage-free.
    scale_cache: dict[tuple[str, str, str, str], float] = {}

    def scale_for(country: str, category: str, table: str, origin: str) -> float:
        key = (country, category, table, origin)
        if key not in scale_cache:
            try:
                s = dataset.load_series(country, category, table)
                cutoff = pd.Timestamp(origin) + pd.offsets.MonthBegin(1)  # incluye el mes de origen
                scale_cache[key] = metrics.naive_scale_before(s, cutoff)
            except Exception as e:  # noqa: BLE001
                # B4: el fallback silencioso scale=1.0 convertía el MASE prospectivo en
                # días crudos (~10³) y fluía a key_facts→web/LaTeX/paper sin señal.
                # NaN excluye la fila del MASE (pandas mean omite NaN) sin perder su
                # cobertura; el conteo se reporta en el meta como n_no_scale.
                log.warning("sin escala para %s/%s/%s@%s: %s — fila sin MASE", country, category, table, origin, e)
                scale_cache[key] = float("nan")
        return scale_cache[key]

    scored, pending = _score_rows(fc, actuals, scale_for)

    sdf = pd.DataFrame(scored)
    sdf.to_csv(REPORTS / "prospective" / "forecast_scorecard.csv", index=False)
    _wrote(tracked, REPORTS / "prospective" / "forecast_scorecard.csv")
    n_no_scale = int(sdf["scaled_err"].isna().sum()) if len(sdf) else 0
    if n_no_scale:
        log.warning("%d fila(s) evaluable(s) sin escala naïve válida (excluidas del MASE)", n_no_scale)
        tracked.warn(f"{n_no_scale} fila(s) evaluable(s) sin escala naïve válida (excluidas del MASE)")
    tracked.log_metrics({"n_scored": int(len(sdf)), "n_pending": int(pending), "n_no_scale": n_no_scale})

    # A3: overall/by_horizon/by_table se ANCLAN al modo backfill — cuando las añadas live
    # empiecen a puntuar viven en by_mode; ningún agregado combina modos jamás.
    back, by_mode = _mode_blocks(sdf)
    overall = _agg(back) if len(back) else {"n": 0}
    by_h = {int(h): _agg(g) for h, g in back.groupby("h")} if len(back) else {}
    by_table = {t: _agg(g) for t, g in back.groupby("table")} if len(back) else {}
    # cov80 HELD-OUT: cobertura de la banda 80 % sobre las añadas NO usadas para calibrar
    # BAND80_RATIO → out-of-sample, no circular (overall.cov80 sí incluye calibración).
    if len(back):
        tracked.log_metrics(_finite({k: overall[k] for k in ("mae_days", "mase", "cov80", "cov95")}))
    heldout = back[~back["origin"].isin(config.BAND80_CAL_VINTAGES)] if len(back) else back
    # n efectivo por añada: muchas añadas (orígenes con último-F antiguo) NO aportan filas
    # evaluables (sus meses-objetivo caen en régimen C/U) → honestidad: el grueso del n
    # viene de pocas añadas recientes. Se reporta el desglose para no inflar la amplitud.
    scored_by_vintage = {o: int((sdf["origin"] == o).sum()) for o in sorted(fc["origin"].unique())} if len(sdf) else {}
    meta = {
        "what": "evaluación prospectiva (pronóstico congelado vs corte realmente publicado)",
        "caveat": "backfill leakage-free; NO equivale a haber servido los pronósticos en tiempo real",
        "aggregation_scope": (
            "overall/by_horizon/by_table = SOLO filas evaluation_mode=backfill (A3); "
            "las añadas live se reportan aparte en by_mode y JAMÁS se agregan junto al backfill"
        ),
        "by_mode": by_mode,
        "n_scored": int(len(sdf)),
        "n_no_scale": n_no_scale,
        "n_pending": int(pending),
        "n_vintages_total": int(fc["origin"].nunique()),
        "n_vintages_effective": int(sum(1 for c in scored_by_vintage.values() if c > 0)),
        "scored_by_vintage": scored_by_vintage,
        "vintages": sorted(fc["origin"].unique().tolist()),
        "overall": overall,
        "by_horizon": by_h,
        "by_table": by_table,
        "band80_calibration": {
            "cal_vintages": list(config.BAND80_CAL_VINTAGES),
            "ratio": config.BAND80_RATIO,
            "n_heldout": int(len(heldout)),
            "cov80_heldout": round(float(heldout["in80"].mean()), 3) if len(heldout) else None,
            "cov80_heldout_ci95": (
                [round(c, 3) for c in intervals.jeffreys_ci(int(heldout["in80"].sum()), len(heldout))]
                if len(heldout)
                else None
            ),
            "insufficient_n": len(heldout) < N_FLOOR,
            "note": "BAND80_RATIO se calibra en cal_vintages; cov80_heldout es la cobertura 80 % OUT-OF-SAMPLE (overall.cov80 incluye la calibración y es optimista).",
        },
    }
    (REPORTS / "prospective" / "forecast_scorecard_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n"
    )
    _wrote(tracked, REPORTS / "prospective" / "forecast_scorecard_meta.json")

    # A3: el ledger sombra se puntúa con la MISMA maquinaria y el mismo universo de
    # actuals, a archivos propios (jamás se mezcla con el scorecard del campeón), y se
    # emite la comparación campeón-vs-sombra por pares para el gate de promoción (A4).
    shadow_path = REPORTS / "prospective" / "forecast_log_shadow.csv"
    if shadow_path.exists():
        sfc = pd.read_csv(shadow_path)
        s_scored, s_pending = _score_rows(sfc, actuals, scale_for)
        s_sdf = pd.DataFrame(s_scored)
        s_sdf.to_csv(REPORTS / "prospective" / "forecast_scorecard_shadow.csv", index=False)
        _wrote(tracked, REPORTS / "prospective" / "forecast_scorecard_shadow.csv")
        _s_back, s_by_mode = _mode_blocks(s_sdf)
        shadow_meta = {
            "what": "scoring del ledger SOMBRA (retador) con la misma maquinaria y universo que el campeón (A3)",
            "caveat": "backfill leakage-free; NO equivale a haber servido los pronósticos en tiempo real",
            "n_scored": int(len(s_sdf)),
            "n_pending": int(s_pending),
            "by_mode": s_by_mode,
            "recipes": sorted(sfc["recipe"].astype(str).unique().tolist()) if "recipe" in sfc.columns else [],
        }
        (REPORTS / "prospective" / "forecast_scorecard_shadow_meta.json").write_text(
            json.dumps(shadow_meta, ensure_ascii=False, indent=2) + "\n"
        )
        _wrote(tracked, REPORTS / "prospective" / "forecast_scorecard_shadow_meta.json")
        h2h = {
            "what": (
                "campeón vs sombra por pares (misma añada/serie/target/h y MISMO "
                "evaluation_mode) — insumo del gate de promoción (A4)"
            ),
            **_head_to_head(sdf, s_sdf),
        }
        (REPORTS / "prospective" / "prospective_head_to_head.json").write_text(
            json.dumps(h2h, ensure_ascii=False, indent=2) + "\n"
        )
        _wrote(tracked, REPORTS / "prospective" / "prospective_head_to_head.json")
        log.info(
            "SOMBRA: n=%d puntuadas (%d pendientes) · head-to-head: %d pares (%d de modo mixto excluidos)",
            len(s_sdf),
            s_pending,
            h2h["n_pairs"],
            h2h["n_mixed_mode_excluded"],
        )
        tracked.log_metrics({"n_pairs": int(h2h["n_pairs"]), "n_pairs_live": _live_pairs(sdf, s_sdf)})

    # D7: UN solo record por invocación (lo emite ``track_run`` al salir de ``run``); antes
    # se emitía uno global MÁS uno por horizonte, así que el número de records dependía
    # del mes y ninguno existía cuando no había nada puntuado o el scoring fallaba.
    tracked.params["n_vintages"] = int(fc["origin"].nunique())
    if len(sdf):
        log.info(
            "PROSPECTIVO: n=%d · MAE %.0f d · MASE %.3f · cob95 %.0f%%",
            overall["n"],
            overall["mae_days"],
            overall["mase"],
            overall["cov95"] * 100,
        )
    else:
        log.info("PROSPECTIVO: 0 objetivos realizados aún (%d pendientes) — se acumula con cada boletín", pending)
    return REPORTS / "prospective" / "forecast_scorecard.csv"


def demo() -> None:
    """Self-check de la lógica de scoring con datos sintéticos (sin BD ni modelos)."""
    fc = pd.DataFrame(
        [
            # objetivo realizado, real dentro de ambas bandas, error 10 d, escala 100 → MASE 0.1
            {
                "origin": "2024-01",
                "h": 1,
                "country": "mexico",
                "category": "F1",
                "table": "FAD",
                "date": "2024-02-01",
                "days": 1000,
                "lo80": 950,
                "hi80": 1050,
                "lo95": 900,
                "hi95": 1100,
            },
            # objetivo realizado, real FUERA de la banda 80 pero dentro de 95
            {
                "origin": "2024-01",
                "h": 2,
                "country": "mexico",
                "category": "F1",
                "table": "FAD",
                "date": "2024-03-01",
                "days": 1000,
                "lo80": 990,
                "hi80": 1010,
                "lo95": 900,
                "hi95": 1100,
            },
            # objetivo aún no realizado → pendiente
            {
                "origin": "2024-01",
                "h": 3,
                "country": "mexico",
                "category": "F1",
                "table": "FAD",
                "date": "2099-01-01",
                "days": 1000,
                "lo80": 950,
                "hi80": 1050,
                "lo95": 900,
                "hi95": 1100,
            },
        ]
    )
    actuals = {
        ("mexico", "F1", "FAD", "2024-02-01"): 1010.0,  # |error|=10
        ("mexico", "F1", "FAD", "2024-03-01"): 1060.0,  # |error|=60, fuera de [990,1010], dentro de [900,1100]
    }
    scored, pending = _score_rows(fc, actuals, lambda *_: 100.0)
    assert pending == 1, pending
    assert len(scored) == 2, len(scored)
    assert scored[0]["abs_err"] == 10 and abs(scored[0]["scaled_err"] - 0.1) < 1e-9
    assert scored[0]["in80"] == 1 and scored[0]["in95"] == 1
    assert scored[1]["in80"] == 0 and scored[1]["in95"] == 1  # cobertura 80 distingue de 95
    print("OK — score_forecasts: pendientes y cobertura 80/95 + MASE correctos")


if __name__ == "__main__":
    import sys

    (demo if "--demo" in sys.argv else run)()
