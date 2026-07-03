# Evaluación prospectiva de pronósticos

Mide qué tan buenos son los pronósticos **congelados** del demostrador web contra lo
que el Visa Bulletin realmente publica después. Complementa al MASE *retrospectivo*
del entregable (hold-out) con la única medida honesta del desempeño a 12 meses en el
mundo real.

## Idea

1. Cada vez que se genera un pronóstico se **archiva** con su `origin` (mes desde el
   que se predijo) y su horizonte `h` (1…12) en un **ledger append-only**.
2. Conforme llegan boletines reales, cada predicción se compara con el **corte
   realmente publicado** en ese mes-objetivo (panel estado `F`).
3. Se acumulan métricas por horizonte: error absoluto, MASE (escala naïve estacional
   in-sample hasta el origen, leakage-free) y cobertura de las bandas 80 % / 95 %.

## Componentes

| Pieza | Qué hace |
|---|---|
| `experiments/generate_web_forecasts.py` | Genera la añada y la archiva en `reports/forecast_log.csv`. Sin argumentos = añada **en vivo** (sirve la web). Con `YYYY-MM` = añada **histórica** leakage-free (trunca la serie a ese mes con `as_of`). |
| `reports/forecast_log.csv` | Ledger inmutable: `origin, h, country, category, table, date, days, lo80, hi80, lo95, hi95`. Idempotente (dedup por `origin+serie+fecha`). |
| `experiments/score_forecasts.py` (`make score-forecasts`) | Califica el ledger vs los cortes reales (`dataset.actuals_F()`). Escribe `reports/forecast_scorecard.csv` (una fila por predicción evaluable) + `_meta.json` (agregados global / por horizonte / por tabla). Tracking MLflow (`web_forecast_scoring`) es local-dev; el registro durable es el scorecard en git. `--demo` corre un self-check sintético. |
| `experiments/backfill_vintages.sh` | Siembra **reproducible**: añada en vivo + añadas históricas (`2024-07, 2025-01, 2025-07`) + scoring. Todo sale del pipeline, sin parches. |

## Modelo de producción (por qué NO es el ganador del entregable)

El entregable concluye que el **deep global (BiTCN)** gana en DFF y **queda apenas por
detrás** en FAD (AutoBiTCN 0.121 ± 0.008; su IC roza el listón ETS/Theta 0.113–0.114 sin
que la media lo alcance). Aun así, el demostrador web
sirve **mediana(Theta + ETS + SARIMA)** en FAD y **SARIMA** en DFF. Es una decisión
deliberada, no un descuido:

- **En FAD el deep no aporta ventaja** (la parsimonia conserva un margen pequeño).
- **El deep necesita GPU + un segundo venv** (`ante_nf`, neuralforecast, pandas<3); el
  Action **semanal que regenera los pronósticos corre en CPU** (torch-CPU) y debe ser
  reproducible y barato. Servir BiTCN exigiría GPU en CI → frágil y costoso.
- La **mediana de 3 estadísticos** es determinista, robusta (sabiduría de las M-competitions),
  CPU-cheap, y cae dentro del empate FAD; **SARIMA** es el mejor desplegable-en-CPU para DFF.

Es decir: la producción **cambia una diferencia FAD estadísticamente insignificante por
robustez/reproducibilidad/costo**. El scorecard prospectivo mide al modelo **realmente
desplegado** (no al ganador del benchmark) — que es lo correcto para la demo. *(Si en el
futuro hay GPU en CI, desplegar BiTCN y re-medir es un cambio de una línea en `PROD`.)*

## Reproducir desde cero

```bash
# 0. Prerrequisito en un clon nuevo: construir el panel + el almacén DuckDB.
make install model-install                  # venv + deps de modelado (darts/torch CPU…)
make panel db                               # data/processed/visapredict.duckdb (lo lee el scorer)

# 1. Sembrar el ledger COMPLETO (10 añadas) + scoring, todo del pipeline con semilla fija:
rm -f reports/forecast_log.csv reports/forecast_scorecard*.{csv,json}
bash experiments/backfill_vintages.sh       # añada viva (→7 orígenes) + 3 históricas + scoring (~25 min CPU)

# 2. (opcional) re-derivar el ratio de la banda 80 % en split disjunto:
make derive-band80                          # imprime ratio + cov80 held-out
```

> La añada en vivo produce, por serie, su propio último mes F como origen (de ahí las 7
> añadas "antiguas" 2015-08…2026-07). Las 3 añadas `as_of` son un **backfill leakage-free**
> para tener métricas desde hoy. **Caveat honesto:** un backfill leakage-free **no es lo
> mismo que haber servido esos pronósticos en tiempo real**; de aquí en adelante el ledger
> acumula añadas realmente servidas, mes a mes. El resultado es bit-reproducible salvo
> deriva numérica menor de la optimización de SARIMA.

## Calibración de la banda 80 % (split disjunto — NO circular)

La banda 80 % conforme directa corría estrecha (cobertura prospectiva ~58 %) porque con
residuales de cola pesada el `P80(|resid|)` queda diminuto frente al `P97.5`. Se ancla a
la banda 95 % (bien calibrada) por un factor:

```
half80 = half95 * BAND80_RATIO        # config.BAND80_RATIO
```

**Importante (evita circularidad):** `BAND80_RATIO` se calibra **solo** sobre las añadas
`config.BAND80_CAL_VINTAGES` (`2024-07`, `2025-01`) y la cobertura 80 % se **valida sobre
las añadas restantes (held-out)**. Reportar `cov80` sobre los mismos datos con que se
ajustó el ratio daría `cov80 ≈ 0.80` por construcción (tautológico); por eso el scorecard
expone **`band80_calibration.cov80_heldout`** = cobertura out-of-sample (≈0.81), y `overall.cov80`
queda marcado como optimista (incluye la calibración).

Re-derivar (read-only, imprime el ratio y el `cov80` held-out):

```bash
ante/bin/python experiments/derive_band80_ratio.py
# si el valor difiere de config.BAND80_RATIO, actualízalo en vp_model/config.py y regenera
```

La banda 95 % no se toca: es el intervalo split-conforme nativo; su cobertura prospectiva
(≈0.92) está **bajo** el nominal del 95 % y se reporta tal cual (ver Limitaciones abajo).

## Automatización

El Action semanal `freeze_and_rebuild.yml`, ante un boletín nuevo: reconstruye el panel
→ **califica** las añadas previas contra el corte real fresco (`score_forecasts`) →
genera la nueva añada (`generate_web_forecasts`) → commitea ledger + scorecard → dispara
el redeploy de la web. La medición se acumula sola.

## Surfaceado en la web

`VisaPredictAI_web` baja `forecast_scorecard_meta.json` en build y muestra, bajo cada
fan-chart, una tarjeta "Historial" (global, todas las series) con el error típico a 3/6/12
meses y el acierto de la banda 95 %.

## Limitaciones (declararlas en el paper)

1. **Banda √h = heurística, no garantía conforme.** La garantía split-conforme es de 1 paso;
   ensanchar por √h a 12 meses NO la transfiere a multi-paso. La cobertura real (95 %→≈0.92,
   degradando 0.97→0.88 con el horizonte) se mide empíricamente, no se garantiza.
2. **`cov95 ≈ 0.92` está BAJO el nominal del 95 %** — se reporta tal cual (under-coverage honesta),
   no se ajusta para "verse" en 0.95.
3. **n efectivo = pocas añadas.** El meta lista 10 añadas pero solo ~3 recientes (2024-07, 2025-01,
   2025-07) aportan el grueso del `n_scored`; las demás (orígenes con último-F antiguo) puntúan ~0.
   Ver `n_vintages_effective` y `scored_by_vintage`. No es una prueba multi-época amplia.
4. **Backfill leakage-free ≠ servido en tiempo real.** Las añadas históricas son reconstrucciones
   sin fuga, pero NO se sirvieron en su momento; de aquí en adelante el ledger acumula añadas reales.
5. **`cov80` overall es optimista** (incluye datos de calibración); el número honesto es
   `band80_calibration.cov80_heldout` (out-of-sample).
6. **Determinismo:** semilla fija (`config.seed_everything`), pero la optimización de SARIMA puede
   tener deriva numérica menor entre máquinas → reproducción casi-exacta, no bit-exacta garantizada.
