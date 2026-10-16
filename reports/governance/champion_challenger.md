# Campeón–retador — veredicto de promoción

## FAD — campeón `median(theta+ets+sarima)` (MASE media 0.1519 · mediana 0.1396 · CRPS 41.52 (informativo))

| retador | MASE media | margen vs campeón | Wilcoxon p | Holm p | ¿apto hold-out h=1? |
|---|---|---|---|---|---|
| `naive1` | 0.1401 | +0.0118 | 6e-05 | 0.00042 | **SÍ** |
| `ets` | 0.1514 | +0.0005 | 0.77756 | 0.98418 | no |
| `median(theta+ets)` | 0.1521 | -0.0002 | 0.32806 | 0.98418 | no |
| `theta` | 0.1543 | -0.0024 | 0.07474 | 0.3737 | no |
| `mean(theta+ets+sarima)` | 0.1575 | -0.0056 | 0.23932 | 0.95728 | no |
| `drift` | 0.1586 | -0.0067 | 0.04126 | 0.24756 | no |
| `median(theta+ets+sarima+arima)` | 0.1604 | -0.0085 | 0.48871 | 0.98418 | no |

**Veredicto:** naive1.

## DFF — campeón `sarima` (MASE media 0.1215 · mediana 0.1068 · CRPS 27.86 (informativo))

| retador | MASE media | margen vs campeón | Wilcoxon p | Holm p | ¿apto hold-out h=1? |
|---|---|---|---|---|---|
| `naive1` | 0.1031 | +0.0184 | 0.02686 | 0.1343 | no |
| `median(sarima+ets+theta)` | 0.1067 | +0.0148 | 0.00049 | 0.00294 | **SÍ** |
| `ets` | 0.1106 | +0.0108 | 0.06396 | 0.209 | no |
| `median(sarima+ets)` | 0.115 | +0.0065 | 0.05225 | 0.209 | no |
| `catboost` | 0.1299 | -0.0084 | 0.12939 | 0.209 | no |
| `drift` | 0.1446 | -0.0232 | 0.06396 | 0.209 | no |

**Veredicto:** median(sarima+ets+theta).

> Margen >0 = el retador mejora la MASE media. `Apto hold-out h=1` = Holm-significativo
> + margen material en el hold-out retrospectivo — NO autoriza producción. La
> autorización la da el gate prospectivo pre-registrado (docs/PROMOTION_POLICY.md,
> decisión en reports/governance/promotion_decision.json) sobre pares live
> campeón-vs-sombra, aplicada por un humano con --promote.
