# Política de promoción pre-registrada (A4)

**Pre-registrada el 2026-07-11 con CERO pares live puntuados** (el
`prospective_head_to_head.json` vigente al registrarla tenía `n_pairs=0`): la política se
fijó antes de ver cualquier resultado que pudiera sesgarla. La versión machine-readable y
autoritativa es `vp_model/promotion.py::POLICY` (versionada en git); este documento es su
espejo en prosa. Cambiarla exige editar `POLICY` **antes** de la añada que se pretenda
juzgar, nunca después de ver sus resultados.

## Los dos gates (vocabulario D1)

| Gate | Protocolo | Qué declara | Qué NO hace |
|---|---|---|---|
| Hold-out (Wilcoxon+Holm, h=1) | P2 | `holdout_pass` = "apto en hold-out h=1" (antes "promotable", renombrado por A4) | NO autoriza producción |
| **Prospectivo pre-registrado** (este) | P5/P6 | decisión `promote` · `retain` · `extend-shadow` · `reject` por tabla | NO se aplica solo: la ejecuta un humano |

`run_champion_challenger.py --promote` exige AMBOS: aptitud hold-out **y** decisión
prospectiva `promote` vigente en `reports/governance/promotion_decision.json` (sin
archivo o con otra decisión, se rehúsa: *fail closed*).

## Reglas pre-registradas (v1.0)

- **Solo pares live autorizan** (`evaluation_mode=live` en ambos lados del par —
  protocolo P6). El backfill informa, jamás promueve.
- **Muestra mínima:** ≥3 añadas live distintas con pares y ≥30 pares por tabla y por
  banda de horizonte (h=1–3, 4–6, 7–12). Insuficiente ⇒ `extend-shadow`, **nunca**
  `promote`.
- **Unidad de inferencia = la serie**, no la fila: los pronósticos de orígenes
  consecutivos se traslapan (autocorrelación), así que se toma la mediana de
  `scaled_err` por serie y banda; las réplicas exactas del corte mundial colapsan a una
  (misma convención B2 del hold-out).
- **Hipótesis:** (h=1) el retador no es inferior al campeón; (h=1..12) el retador mejora
  materialmente al campeón en TODAS las bandas. Wilcoxon pareado unilateral por banda +
  Holm entre bandas por tabla, α=0.05.
- **Margen material:** mejora relativa de MASE ≥10 % en cada banda, significativa tras
  Holm. **Retroceso:** si el retador es significativamente peor en alguna banda por más
  de 5 %, `reject` — "significativamente peor" TAMBIÉN bajo Holm (v1.0.1, 11-jul,
  auditoría del autor: la implementación evaluaba `p_worse` crudo en la familia del
  rechazo; corregida a la política registrada con 0 pares live vistos y ningún
  parámetro cambiado).
- **Cobertura:** el intervalo 95 % del retador debe cubrir ≥0.90 en pares live y no
  quedar >3 puntos debajo del campeón; si falla, no hay `promote`.
- **Decisiones:** `promote` (todo lo anterior en regla) · `reject` (retroceso
  significativo) · `extend-shadow` (muestra insuficiente) · `retain` (todo lo demás).

## Aprobación humana y rollback (obligatorios)

La decisión del gate es **consultiva**: el manifiesto campeón
(`champion_manifest.json`) solo cambia cuando un humano corre `--promote` y el gate
autoriza. **Rollback pre-registrado:** el manifiesto está versionado en git — revertir el
commit de promoción y redesplegar el demostrador restaura al campeón anterior; los
ledgers (append-only, v2) conservan intactas las añadas de ambos.

## Operación

- `experiments/run_promotion_gate.py` corre tras `score_forecasts.py` en el cron y
  refresca `reports/governance/promotion_decision.json` (decisión + razones + política
  íntegra, auditable).
- Estado al pre-registro: retador `naive1` en sombra desde jul-2026; 0 pares live
  puntuados; decisión esperada durante la acumulación: `extend-shadow`.
