# Campeón–retador — veredicto de promoción

## FAD — campeón `median(theta+ets+sarima)` (MASE media 0.1206 · mediana 0.1075 · CRPS 32.13 (informativo))

| retador | MASE media | margen vs campeón | Wilcoxon p | Holm p | ¿apto hold-out h=1? |
|---|---|---|---|---|---|
| `naive1` | 0.1046 | +0.0160 | 6e-05 | 0.00042 | **SÍ** |
| `ets` | 0.1192 | +0.0014 | 0.4212 | 1.0 | no |
| `median(theta+ets)` | 0.1208 | -0.0002 | 0.59949 | 1.0 | no |
| `theta` | 0.1239 | -0.0033 | 0.24549 | 1.0 | no |
| `drift` | 0.1287 | -0.0081 | 0.02557 | 0.15342 | no |
| `median(theta+ets+sarima+arima)` | 0.1292 | -0.0086 | 0.97797 | 1.0 | no |
| `mean(theta+ets+sarima)` | 0.1551 | -0.0345 | 0.45428 | 1.0 | no |

**Veredicto:** naive1.

## DFF — campeón `sarima` (MASE media 0.0996 · mediana 0.1076 · CRPS 31.33 (informativo))

| retador | MASE media | margen vs campeón | Wilcoxon p | Holm p | ¿apto hold-out h=1? |
|---|---|---|---|---|---|
| `naive1` | 0.0773 | +0.0223 | 0.00195 | 0.0117 | **SÍ** |
| `median(sarima+ets+theta)` | 0.0856 | +0.0140 | 0.00195 | 0.0117 | **SÍ** |
| `ets` | 0.0915 | +0.0081 | 0.16016 | 0.52344 | no |
| `median(sarima+ets)` | 0.0942 | +0.0054 | 0.13086 | 0.52344 | no |
| `catboost` | 0.106 | -0.0064 | 0.43164 | 0.52344 | no |
| `drift` | 0.1186 | -0.0191 | 0.16016 | 0.52344 | no |

**Veredicto:** naive1.

> Margen >0 = el retador mejora la MASE media. `Apto hold-out h=1` = Holm-significativo
> + margen material en el hold-out retrospectivo — NO autoriza producción. La
> autorización la da el gate prospectivo pre-registrado (docs/PROMOTION_POLICY.md,
> decisión en reports/governance/promotion_decision.json) sobre pares live
> campeón-vs-sombra, aplicada por un humano con --promote.
