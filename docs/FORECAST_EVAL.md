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
| `experiments/score_forecasts.py` (`make score-forecasts`) | Califica el ledger vs los cortes reales (`dataset.actuals_F()`). Escribe `reports/forecast_scorecard.csv` (una fila por predicción evaluable) + `_meta.json` (agregados global / por horizonte / por tabla). Registra en MLflow (`web_forecast_scoring`). `--demo` corre un self-check sintético. |
| `experiments/backfill_vintages.sh` | Siembra **reproducible**: añada en vivo + añadas históricas (`2024-07, 2025-01, 2025-07`) + scoring. Todo sale del pipeline, sin parches. |

## Reproducir desde cero

```bash
rm -f reports/forecast_log.csv reports/forecast_scorecard*.{csv,json}
bash experiments/backfill_vintages.sh      # añada viva + históricas + scoring (~25 min CPU)
# o, paso a paso:
make web-forecasts                          # añada en vivo (sirve la web)
ante/bin/python experiments/generate_web_forecasts.py 2025-07   # una añada histórica
make score-forecasts                        # califica y escribe el scorecard
```

> Las añadas anteriores al último boletín son un **backfill leakage-free** para tener
> métricas desde hoy; de aquí en adelante el ledger acumula añadas reales mes a mes.

## Calibración de la banda 80 %

La banda 80 % conforme directa corría estrecha (cobertura prospectiva ~58 %) porque con
residuales de cola pesada el `P80(|resid|)` queda diminuto frente al `P97.5`. Se ancla a
la banda 95 % (bien calibrada, cobertura 95 %) por un factor calibrado en datos:

```
half80 = half95 * BAND80_RATIO        # BAND80_RATIO = 0.4655 en generate_web_forecasts.py
```

`BAND80_RATIO = P80(|error| / half95)` sobre las observaciones prospectivas. **Re-derivarlo
periódicamente** cuando el scorecard crezca (si `cov80` se aleja de 0.80):

```python
# con reports/forecast_log.csv y dataset.actuals_F():  ratio = quantile_0.80(|pred-actual| / ((hi95-lo95)/2))
```

La banda 95 % no se toca (cobertura prospectiva 95 %, bien calibrada).

## Automatización

El Action semanal `freeze_and_rebuild.yml`, ante un boletín nuevo: reconstruye el panel
→ **califica** las añadas previas contra el corte real fresco (`score_forecasts`) →
genera la nueva añada (`generate_web_forecasts`) → commitea ledger + scorecard → dispara
el redeploy de la web. La medición se acumula sola.

## Surfaceado en la web

`VisaPredictAI_web` baja `forecast_scorecard_meta.json` en build y muestra, bajo cada
fan-chart, una tarjeta "Historial" con el error típico a 3/6/12 meses y el acierto de la
banda 95 %.
