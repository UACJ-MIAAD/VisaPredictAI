# Model Card — VisaPredict AI

> Tarjeta auto-generada por `experiments/build_model_card.py` a partir de los artefactos
> canónicos. **No editar a mano** — se regenera con `make model-card`. Toda cifra proviene de
> `reports/key_facts.json` (fuente única de verdad).

## 1. Detalles del modelo
- **Sistema:** predictor del U.S. Visa Bulletin — panel multiserie `y_{p,c,b,t}` (país × categoría × tabla × mes).
- **Tarea:** regresión temporal de fechas de prioridad sobre observaciones con estado **F** (FAD y DFF por separado).
- **Receta desplegada (campeón):** FAD → `median(theta+ets+sarima)` · DFF → `sarima` (manifiesto versionado `champion_manifest.json`).
- **Versión / linaje:** git `d2d6833` · hash del panel `6f25df6b8f09`.
- **Autor:** Javier A. Rebull Saucedo · MIAAD, UACJ. Demostrador: visapredictai.com.

## 2. Uso previsto
- **Sí:** demostrar pronóstico con intervalos al 95 %/80 % para fines académicos y de exploración.
- **No:** asesoría legal/migratoria ni garantía de fechas. El sistema **no** predice cambios de régimen (C↔F↔U).

## 3. Factores
- País o área de cargabilidad: México, India, China, Filipinas, Resto del mundo.
- Categorías: familiares (F1–F4) y empleo (EB). Tablas: Final Action Dates (FAD) y Dates for Filing (DFF), evaluadas por separado.

## 4. Datos de entrenamiento
- **Panel:** 27,289 observaciones · 58 % entrenables (estado F = 15,758) · rango 2001-12 → 2026-07.
- **Series:** 194 estructurales · 74 plenamente evaluables (≥84 obs F = ventana 60 + hold-out 24).
- Fuente: U.S. Department of State, Visa Bulletin (HTML congelado, parseo offline reproducible).

## 5. Evaluación
**Hold-out leakage-free (MASE media):** FAD campeón `median(theta+ets+sarima)` = **0.1116** · DFF campeón `sarima` = **0.0996**.
**Model Confidence Set (90 %):** FAD = {ets, theta} · DFF = {ets, theta} (Friedman–Nemenyi).
**Prospectiva (ledger congelado, mundo real):** n=2944 · MAE=146 días · MASE=0.345 · cobertura 95 %=0.92 · 80 % (out-of-sample)=0.81.

## 6. Linaje y reproducibilidad
- **Receta:** `champion_manifest.json` (cambia solo vía `run_champion_challenger.py --promote`, auditado).
- **Código:** git `d2d6833`. **Datos:** panel hash `6f25df6b8f09`. **Pipeline:** `dvc repro` (DAG determinista, `dvc.lock`).
- **Promoción:** gateada por Wilcoxon+Holm en hold-out; confirmación prospectiva requiere despliegue en sombra.

## 7. Limitaciones y consideraciones éticas
- El borde del modelado profundo sobre los clásicos es **modesto y frágil** (sensible a agregación; muestra DFF efectiva pequeña).
- Solo modela estado F; C/U son anotación descriptiva, no objetivo.
- Las retrogresiones por cuota son reales y el modelo debe tolerarlas; no constituye consejo legal.
