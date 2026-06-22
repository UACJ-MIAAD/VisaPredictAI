# Campeón–retador — veredicto de promoción

## FAD — campeón `median(theta+ets+sarima)` (MASE media 0.1116 · mediana 0.1019)

| retador | MASE media | margen vs campeón | Wilcoxon p | Holm p | ¿promovible? |
|---|---|---|---|---|---|
| `theta` | 0.1114 | +0.0002 | 0.05875 | 0.17625 | no |
| `median(theta+ets)` | 0.1118 | -0.0003 | 0.31731 | 0.63462 | no |
| `ets` | 0.1123 | -0.0007 | 0.49079 | 0.63462 | no |
| `mean(theta+ets+sarima)` | 0.1164 | -0.0049 | 0.01193 | 0.05965 | no |
| `median(theta+ets+sarima+arima)` | 0.119 | -0.0074 | 0.03181 | 0.12724 | no |

**Veredicto:** ninguno — se mantiene el campeón.

## DFF — campeón `sarima` (MASE media 0.0996 · mediana 0.1051)

| retador | MASE media | margen vs campeón | Wilcoxon p | Holm p | ¿promovible? |
|---|---|---|---|---|---|
| `median(sarima+ets)` | 0.0971 | +0.0025 | 0.16138 | 0.64552 | no |
| `ets` | 0.0983 | +0.0013 | 0.57544 | 1.0 | no |
| `median(sarima+ets+theta)` | 0.0989 | +0.0007 | 0.44409 | 1.0 | no |
| `catboost` | 0.1047 | -0.0051 | 0.82446 | 1.0 | no |

**Veredicto:** ninguno — se mantiene el campeón.

> Margen >0 = el retador mejora la MASE media. La promoción exige Holm-significancia
> + margen material. La confirmación PROSPECTIVA (ledger congelado) requiere despliegue
> en sombra del retador; hoy el ledger solo califica al campeón desplegado.
