# Campeón–retador — veredicto de promoción

## FAD — campeón `median(theta+ets+sarima)` (MASE media 0.1183 · mediana 0.0955)

| retador | MASE media | margen vs campeón | Wilcoxon p | Holm p | ¿promovible? |
|---|---|---|---|---|---|
| `theta` | 0.1175 | +0.0008 | 0.22931 | 1.0 | no |
| `median(theta+ets)` | 0.1185 | -0.0002 | 0.48871 | 1.0 | no |
| `ets` | 0.1195 | -0.0012 | 1.0 | 1.0 | no |
| `mean(theta+ets+sarima)` | 0.1249 | -0.0066 | 0.3028 | 1.0 | no |
| `median(theta+ets+sarima+arima)` | 0.1293 | -0.0110 | 0.22931 | 1.0 | no |

**Veredicto:** ninguno — se mantiene el campeón.

## DFF — campeón `sarima` (MASE media 0.0996 · mediana 0.1076)

| retador | MASE media | margen vs campeón | Wilcoxon p | Holm p | ¿promovible? |
|---|---|---|---|---|---|
| `ets` | 0.0935 | +0.0061 | 0.10547 | 0.25194 | no |
| `median(sarima+ets+theta)` | 0.0946 | +0.0050 | 0.08398 | 0.25194 | no |
| `median(sarima+ets)` | 0.0955 | +0.0041 | 0.03711 | 0.14844 | no |
| `catboost` | 0.105 | -0.0054 | 1.0 | 1.0 | no |

**Veredicto:** ninguno — se mantiene el campeón.

> Margen >0 = el retador mejora la MASE media. La promoción exige Holm-significancia
> + margen material. La confirmación PROSPECTIVA (ledger congelado) requiere despliegue
> en sombra del retador; hoy el ledger solo califica al campeón desplegado.
