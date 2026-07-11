# Evaluación de pronósticos: matriz de protocolos y registro prospectivo

Mide qué tan buenos son los pronósticos del demostrador web contra lo que el Visa
Bulletin realmente publica después. Complementa al MASE *retrospectivo* del
entregable (hold-out) con la medida out-of-sample del desempeño a 12 meses.
**Precisión A1 (plan auditoría 2026-07-11):** el registro puntuado a la fecha es un
**backfill sin fuga de información** (cada pronóstico usa solo información disponible
hasta su origen, pero no fue servido en tiempo real); las añadas servidas en vivo se
congelan al emitirse desde el despliegue de julio de 2026. Los claims deben respetar
esa distinción.

## Matriz de protocolos (vocabulario congelado — D1)

Cada cifra publicada debe declarar de qué protocolo sale, con su población, horizonte,
`n` y vintage. Los nombres de esta tabla son canónicos: no mezclar universos ni
renombrar sin actualizar esta matriz.

| # | Protocolo (nombre canónico) | Población | Horizonte | Métricas | n / vintage | Uso decisional | Qué NO autoriza |
|---|---|---|---|---|---|---|---|
| P1 | **Selección walk-forward** (región de selección) | 74 series evaluables (≥84 obs F), por tabla | h=1 expandiendo | MASE por serie (media/mediana) | según serie; corte = panel del build | Selección de modelos e hiperparámetros; listón de referencia | Claims de producto; comparación final; lenguaje prospectivo |
| P2 | **Hold-out de confirmación** (24 m reservados) | mismas series evaluables, por tabla | h=1 | MASE media/mediana · DM+Holm · Friedman–Nemenyi · MCS 90 % | 24 m finales por serie; vintage = panel del build | Veredicto comparativo del entregable; gate campeón-retador (Wilcoxon+Holm) | Promoción de serving (el producto sirve h=1..12); lenguaje prospectivo |
| P3 | **Rolling multi-horizonte retrospectivo** (GPU, Apéndice A.10) | celdas familia/empleo × FAD/DFF | h=1..36 (meses calendario) | MASE por horizonte | corridas dedicadas; cutoffs rolling | Análisis del cruce de horizonte (los clásicos baten al RW ~20–25 % a 12 m) | Serving; conclusiones de época única |
| P4 | **Backfill pseudo-prospectivo** (scorecard) | universo servido por el demostrador | h=1..12 | MASE/MAE/cobertura 80–95 % por horizonte | 2,944 scored; 10 añadas, **3 efectivas** (2024-07, 2025-01, 2025-07) | Evidencia multi-horizonte del producto, **siempre con el caveat A1** | Claims de "servido/congelado en tiempo real" |
| P5 | **Registro sombra** (retador naïve-1) | mismo universo servido | h=1..12 | las de P4 cuando se unifique el scoring (A3) | añadas congeladas por el cron desde jul-2026 | Insumo futuro del gate de promoción (A4) | Cualquier claim (aún sin scoring comparable) |
| P6 | **Prospectivo real** (añadas servidas en vivo) | universo servido | h=1..12 | las de P4 | añadas emitidas y congeladas desde jul-2026; targets aún futuros (n scored ≈ 0) | ÚNICO protocolo que autoriza claims de servicio en tiempo real; base de la decisión de promoción (A4) | Nada lo sustituye retroactivamente: una fila es P6 solo con prueba de nacimiento anterior al target |

**Vocabulario congelado:** *backfill sin fuga* (P4) · *añada servida en vivo* (P6) ·
*hold-out de confirmación* (P2) · *región de selección* (P1) · *registro sombra* (P5) ·
*promovible* = veredicto retrospectivo del gate en P2, **no** una autorización de
despliegue (la promoción se decide con P5/P6, política A4).

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
| `experiments/generate_web_forecasts.py` | Genera la añada y la archiva en `reports/prospective/forecast_log.csv`. Sin argumentos = añada **en vivo** (sirve la web). Con `YYYY-MM` = añada **histórica** leakage-free (trunca la serie a ese mes con `as_of`). |
| `reports/prospective/forecast_log.csv` | **Ledger v2** inmutable (A2, 11-jul-2026): columnas originales + identidad de freeze de `vp_model/ledger.py` (`forecast_id` determinista, `frozen_at` UTC, `freeze_panel_vintage`, `panel_hash`, `git_sha`, `model_version`, `evaluation_mode`). Idempotente (dedup por `origin+serie+fecha`, keep-first). Las filas históricas llevan su **acta de nacimiento derivada del git log** (`experiments/migrate_ledger_v2.py`): `evaluation_mode=live` SOLO si el target era posterior al vintage del panel al congelar (999 filas del campeón lo prueban); el resto es `backfill`. `ledger.validate()` caza manipulación temporal y de contenido. `release_id`/`deployment_id` llegan con B1 como migración aditiva. |
| `experiments/score_forecasts.py` (`make score-forecasts`) | Califica el ledger vs los cortes reales (`dataset.actuals_F()`). Escribe `reports/prospective/forecast_scorecard.csv` (una fila por predicción evaluable) + `_meta.json` (agregados global / por horizonte / por tabla). Tracking MLflow (`web_forecast_scoring`) es local-dev; el registro durable es el scorecard en git. `--demo` corre un self-check sintético. |
| `experiments/backfill_vintages.sh` | Siembra **reproducible**: añada en vivo + añadas históricas (`2024-07, 2025-01, 2025-07`) + scoring. Todo sale del pipeline, sin parches. |

## Modelo de producción (por qué NO es el "ganador" del hold-out)

Tras la re-campaña AQ (catálogo de 24 modelos, jul-2026), el veredicto del hold-out de
confirmación (P2) es un **piso**: a un paso, el **naïve-1 (random walk)** es el modelo a
vencer y ninguno lo supera con significancia — el MCS al 90 % retiene **solo al naïve-1
en ambas tablas** (FAD 0.100 media / 0.089 mediana; DFF 0.086, empate exacto con Theta).
El mejor profundo (AutoBiTCN FAD 0.109 ± 0.007) queda por debajo de ETS/Theta pero por
encima del piso. Aun así, el demostrador sirve **mediana(Theta + ETS + SARIMA)** en FAD
(hold-out 0.121) y **SARIMA** en DFF (0.100). Es una decisión deliberada:

- **El gate mide h=1; el producto sirve h=1..12.** El naïve-1 entrega una constante que
  se degrada conforme las colas se mueven; el campeón sigue la trayectoria en todo el
  horizonte (ahí vive el valor del sistema: P4 muestra la degradación ordenada).
- El naïve-1 **pasó el gate retrospectivo** (Wilcoxon+Holm) y quedó **promovible a h=1**,
  pero la promoción está **retenida**: se decidirá con la evidencia multi-horizonte del
  registro sombra (P5) y de las añadas en vivo (P6), bajo la política pre-registrada A4.
- La **mediana de 3 estadísticos** es determinista, robusta (sabiduría de las
  M-competitions) y CPU-cheap: el Action que regenera los pronósticos corre en CPU y debe
  ser reproducible y barato; los profundos exigen GPU + un segundo venv (`ante_nf`).

El scorecard (P4/P6) mide al modelo **realmente desplegado** (no al ganador de un corte
retrospectivo) — que es lo correcto para el producto.

## Reproducir desde cero

```bash
# 0. Prerrequisito en un clon nuevo: construir el panel + el almacén DuckDB.
make install model-install                  # venv + deps de modelado (darts/torch CPU…)
make panel db                               # data/processed/visapredict.duckdb (lo lee el scorer)

# 1. Sembrar el ledger COMPLETO (10 añadas) + scoring, todo del pipeline con semilla fija:
rm -f reports/prospective/forecast_log.csv reports/prospective/forecast_scorecard*.{csv,json}
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

## Bandas por horizonte (método desplegado) y calibración de la banda 80 %

**Método desplegado:** el semiancho por horizonte h=1..12 se escala con los **cuantiles
empíricos por horizonte** del propio registro (`reports/prospective/pi_scale_by_h.json`,
por tabla y nivel 80/95 %), con ajuste **ACI** (inferencia conforme adaptativa) en línea.
El mecanismo de esta sección (ratio escalar calibrado en split disjunto) es hoy el
**fallback** cuando falta el archivo de escalas; cada fila del ledger marca en
`band_method` cuál se usó.

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

El Action `freeze_and_rebuild.yml` (L-V, S3-driven), ante un boletín nuevo: reconstruye el panel
→ **califica** las añadas previas contra el corte real fresco (`score_forecasts`) →
genera la nueva añada (`generate_web_forecasts`) → commitea ledger + scorecard → dispara
el redeploy de la web. La medición se acumula sola.

## Surfaceado en la web

`VisaPredictAI_web` baja `forecast_scorecard_meta.json` en build y muestra, bajo cada
fan-chart, una tarjeta "Historial" (global, todas las series) con el error típico a 3/6/12
meses y el acierto de la banda 95 %.

## Limitaciones (declararlas en el paper)

1. **Bandas por horizonte = cuantiles empíricos, no garantía conforme multi-paso.** La
   garantía split-conforme es de 1 paso; el escalado por horizonte usa cuantiles empíricos
   del propio registro (+ ACI) y su cobertura real (95 %→≈0.92, degradando 0.97→0.88 con el
   horizonte) se mide empíricamente, no se garantiza. El fallback de raíz del horizonte solo
   aplica si falta `pi_scale_by_h.json` y queda marcado en `band_method`.
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
