# Propuesta de paquete para el paper (MICAI) — BORRADOR, requiere revisión del asesor

> ⚠️ **Esto NO es el `.tex` oficial.** Es una propuesta de remediación derivada de la
> auditoría adversarial (5 revisores). El cambio al deliverable lo decide el autor con el
> Dr. Chente. Aquí se proponen: (1) reencuadre de la contribución, (2) una sección nueva de
> evaluación prospectiva, (3) tablas/citas faltantes, (4) amenazas de validez a declarar,
> (5) qué afirmaciones NO hacer.

## 1. Reencuadre de la contribución (lo más importante)

El paper, como está, se lee como "construimos 21 modelos y el deep gana" → **rechazo de
escritorio** (sin novedad metodológica). Reencuadrar a **paper de aplicación** con 3
contribuciones defendibles:

1. **Dataset público nuevo y auditado** — panel multiserie homogéneo del U.S. Visa Bulletin
   (194 series estructurales, 27,611 obs, dic-2001→2026 completo 296/296), rico en retrogresiones, con
   anotación de régimen C/F/U (MNAR-honesta) y reproducible (HTML congelado en S3 → rebuild
   offline). *Liderar con esto.*
2. **Protocolo de evaluación PROSPECTIVA sobre pronósticos congelados** — la idea novedosa:
   archivar cada pronóstico con su origen y calificarlo contra el corte realmente publicado
   después (leakage-free, no-backfilleable a futuro). *Moverlo al cuerpo como contribución central.*
3. **Resultado contingente/negativo creíble** — el deep global solo supera a la parsimonia en
   régimen DFF (series cortas), no en FAD (largas). Empuja contra el hype; bien soportado.

## 2. Sección nueva propuesta: "Evaluación prospectiva" (mover del repo al `.tex`)

Texto base (ajustar al estilo del deliverable):

> Más allá del hold-out retrospectivo (§2.x), se evaluó el sistema de forma **prospectiva**:
> cada pronóstico se **congela** con su mes de origen y se compara, conforme se publican los
> boletines, contra el corte real. Sobre **N=2944** predicciones evaluables (modelos de
> producción desplegados, no el ganador del benchmark; ver §X), el error absoluto medio fue
> **146 días** y el MASE **0.345**, degradando suavemente con el horizonte (h=1: MAE ~20 d,
> MASE 0.05; h=12: MAE ~351 d, MASE 0.75, aún < 1 = supera al naïve estacional). La banda de
> predicción al 95 % cubrió empíricamente el **92 %** (bajo el nominal; ver Amenazas).

**Tabla propuesta — desempeño prospectivo por horizonte** (datos en `reports/prospective/forecast_scorecard_meta.json`):

| h (meses) | n | MAE (días) | MASE | cobertura 95 % |
|---|---|---|---|---|
| 1 | … | ~20 | ~0.05 | ~0.97 |
| 3 | … | ~106 | ~0.24 | ~0.99 |
| 6 | … | ~157 | ~0.33 | ~0.96 |
| 12 | … | ~351 | ~0.75 | ~0.88 |

*(rellenar con los valores exactos del scorecard; aclarar que MASE retrospectivo 0.117 y
prospectivo 0.345 NO son comparables: 1-paso repetido vs 1–12-pasos genuino).*

## 3. Tablas/citas faltantes que un revisor exigirá

- **Tabla numérica DM/Holm + MCS** para la comparación deep-global vs parsimonia (hoy se
  nombran pero no se muestran p-values ni el conjunto MCS). Datos en `reports/` (significance).
- **Related work faltante:** Montero-Manso & Hyndman 2021 (modelos globales), Makridakis M4/M5
  ("simple supera a complejo"), Monash Forecasting Archive, y prior art de modelado de
  backlogs migratorios.

## 4. Amenazas de validez a DECLARAR (subsección obligatoria)

(ver también `docs/FORECAST_EVAL.md` §Limitaciones)

1. **Bandas √h = heurística, no garantía conforme** (la garantía es 1-paso; cov95 cae 0.97→0.88).
2. **cov95 ≈ 0.92 bajo el nominal** — under-coverage honesta, no ajustada.
3. **n efectivo = 3 añadas recientes** (de 10 listadas; las demás puntúan ~0). No es prueba multi-época.
4. **Backfill leakage-free ≠ servido en tiempo real** (las añadas históricas son reconstrucciones).
5. **Pseudo-replicación**: "Resto del mundo" duplica India/China cuando comparten fecha mundial →
   infla el n efectivo / independencia (multiplicidad). Deduplicar o declararlo.
6. **Dependencia de hardware/semilla** de los resultados deep (un acelerador, 5 semillas, n=25).
7. **Sesgo de supervivencia** (SARIMA/ARIMA convergen en subconjuntos) — ya declarado para DFF.

## 5. Qué NO afirmar (corregido en esta tanda)

- ❌ "cobertura 80 % = 0.80" como resultado → era circular (ratio ajustado in-sample). Reportar
  **`cov80_heldout` (out-of-sample, ≈0.81)** y marcar el overall como optimista.
- ❌ "bandas conformes con garantía a 12 meses" → son 1-paso ⋅ √h (heurístico).
- ❌ "194 series modeladas" → 194 **estructurales**; 74 plenamente evaluables (≥84 obs F).
- ❌ "deep gana DFF 23 %/15 %" → tras añadir **Auto-ARIMA afinado** (media 0.101), la ventaja DFF es **~11 % y frágil** (BiTCN 0.090 vs 0.101); sensible a la agregación (por mediana empata) y sobre ~14 series distintas. FAD = empate. MCS = {ETS, Theta}.
- ❌ "tracked in MLflow" como procedencia de CI → el registro durable es el scorecard en git + git_sha.
- ⚠️ "base de datos 1992–2026" → decisión editorial del autor (marco 1992 + caveat dic-2001 presente);
  para MICAI, considerar decir "dic-2001→2026 (FAD publicadas desde 1992)".

## 6. Veredicto del panel (síntesis)

**Major revision** si se reencuadra como arriba; **desk-reject** si se presenta como paper de
métodos. El fix #1: mover la evaluación prospectiva al cuerpo como contribución central,
reportar cov95=0.92 honesto, y justificar el modelo desplegado (§X / `docs/FORECAST_EVAL.md`).
