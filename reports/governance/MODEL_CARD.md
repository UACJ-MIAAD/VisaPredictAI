# Model Card — VisaPredict AI

> Tarjeta auto-generada por `experiments/build_model_card.py` a partir de los artefactos
> canónicos. **No editar a mano** — se regenera con `make model-card`. Toda cifra proviene de
> `reports/governance/key_facts.json` (fuente única de verdad).

> **Protocolo retrospectivo:** `f2-causal`. Los artefactos de esta tarjeta se acreditaron
> contra la reconciliación `recon_5d208d0_g7_20261019T134251` de la campaña `rederiv_953cd00_20261006T073927`.
> Esa acreditación retrospectiva no autoriza promover un retador: sigue vigente el gate prospectivo.

## 1. Detalles del modelo
- **Sistema:** predictor del U.S. Visa Bulletin — panel multiserie `y_{p,c,b,t}` (país × categoría × tabla × mes).
- **Tarea:** regresión temporal de fechas de prioridad sobre observaciones con estado **F** (FAD y DFF por separado).
- **Receta desplegada (campeón):** FAD → `median(theta+ets+sarima)` · DFF → `sarima` (manifiesto versionado `champion_manifest.json`).
- **Versión / linaje:** git `67ad3fb` · hash del panel `07d43c029953`.
- **Autor:** Javier A. Rebull Saucedo · MIAAD, UACJ. Demostrador: visapredictai.com.

## 2. Uso previsto
- **Sí:** demostrar pronóstico con intervalos al 95 %/80 % para fines académicos y de exploración.
- **No:** asesoría legal/migratoria ni garantía de fechas. El sistema **no** predice cambios de régimen (C↔F↔U).

## 3. Factores
- País o área de cargabilidad: México, India, China, Filipinas, Resto del mundo.
- Categorías: familiares (F1–F4) y empleo (EB). Tablas: Final Action Dates (FAD) y Dates for Filing (DFF), evaluadas por separado.

## 4. Datos de entrenamiento
- **Panel:** 27,911 observaciones · 58 % entrenables (estado F = 16,117) · rango 2001-12 → 2026-09.
- **Series:** 194 estructurales · 74 plenamente evaluables (≥84 obs F = ventana 60 + hold-out 24).
- Fuente: U.S. Department of State, Visa Bulletin (HTML congelado, parseo offline reproducible).

## 5. Evaluación
**Marco comparativo:** 24 modelos evaluados bajo el mismo protocolo walk-forward; de ahí salen el campeón desplegado y su retador.
**Hold-out leakage-free (MASE media):** FAD campeón `median(theta+ets+sarima)` = **0.1519** · DFF campeón `sarima` = **0.1215**.
**Model Confidence Set (90 %):** FAD = {naive1} · DFF = {ets, naive1} (Friedman–Nemenyi).
**Prospectiva (backfill sin fuga de información; añadas servidas en vivo desde jul-2026):** n=2944 · MAE=146 días · MASE=0.347 · cobertura 95 %=0.92 · 80 % (out-of-sample)=0.81.

## 5.1 Cohortes de estabilidad y enrutado (épica E, exploratorio)
- **Partición (regla v1.0.0, pre-registrada):** 74 series evaluables · 39 estables · 35 no estables. Calculada solo con información anterior al hold-out.
- **Barrido de lo ya puntuado:** 0 recetas baten al naïve-1 de su cohorte; 66 quedan por debajo y 24 sin evidencia suficiente.
- **Campaña por cohorte (CPU):** 42 corridas, 42 completadas; 0 de 18 recetas primarias baten ese piso.
- **Enrutado por cohorte:** de 4 celdas, 0 superan el gate (mejora ≥ 0.005 en MASE, contraste bilateral con Holm al 0.05 y sin pérdida material, exigidos en los tres horizontes). Las mejoras largas de FAD no satisfacen el corto plazo (0 horizontes cortos significativos); la cohorte estable de DFF pierde de forma material (-0.0115 a tres meses pese a 0.1352 a doce); y la inestable de DFF, con 5 series, no aporta evidencia.
- **Estado:** exploratorio. El enrutador **no está desplegado ni promovido**: su autorización exigiría sombra prospectiva, igual que cualquier retador.

## 6. Linaje y reproducibilidad
- **Receta:** `champion_manifest.json` (cambia solo vía `run_champion_challenger.py --promote`, auditado).
- **Código:** git `67ad3fb`. **Datos:** panel hash `07d43c029953`. **Pipeline:** `dvc repro` (DAG determinista, `dvc.lock`).
- **Corte (D4):** identidad del corte en el campo estructural de abajo · pipeline_run_id `local` · añada `2026-09`.

```
release_id: 2026-09-805d624d2ecf
```
- **Promoción (dos gates):** el hold-out (Wilcoxon+Holm, h=1) solo declara aptitud retrospectiva; la autorización la da el gate prospectivo PRE-REGISTRADO (docs/PROMOTION_POLICY.md) sobre pares live campeón-vs-sombra, aplicada por un humano (`--promote`, que se rehúsa sin decisión "promote") con rollback versionado.

## 7. Limitaciones y consideraciones éticas
- El borde del modelado profundo sobre los clásicos es **modesto y frágil** (sensible a agregación; muestra DFF efectiva pequeña).
- Solo modela estado F; C/U son anotación descriptiva, no objetivo.
- Las retrogresiones por cuota son reales y el modelo debe tolerarlas; no constituye consejo legal.
- El registro prospectivo actual es un backfill sin fuga (información truncada al origen), no pronósticos servidos en tiempo real; las añadas servidas en vivo se acumulan desde jul-2026.
