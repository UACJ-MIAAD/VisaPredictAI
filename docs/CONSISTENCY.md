# Guardián de consistencia — la máxima del proyecto

> **TODO 100% alineado SIEMPRE.** web · LaTeX entregable · paper MICAI · RAG · README · docs
> deben dar el **mismo número y claim**. Una cifra desalineada es una grieta que un revisor
> explota. Este mecanismo lo **automatiza**: una fuente única de verdad + un linter que rompe
> el build si cualquier artefacto deriva.

## Cómo funciona

```
datos/reportes ──► experiments/build_key_facts.py ──► reports/key_facts.json   (FUENTE DE VERDAD)
                                                  └──► reports/latex/key_facts.tex (macros \factXxx)
                                                              │
tools/consistency_rules.yml ──► tools/check_consistency.py ──┤ compara los artefactos
                                                              ▼
        web / ProyectoI.tex / paper.tex / README / docs  ──► ✓ o ✗ (falla el build)
```

- **`reports/key_facts.json`** — cifras canónicas **computadas del pipeline** (n series, obs,
  %F, rango de fechas, MASE/MAE/cobertura prospectivos, medias por modelo, MCS, márgenes
  deep-vs-parsimonia, BAND80\_RATIO). **No editar a mano** — `make key-facts` lo regenera.
- **`tools/consistency_rules.yml`** — reglas: `forbidden` (claims viejos prohibidos),
  `required` (cifras canónicas que DEBEN aparecer), `numeric` (un número etiquetado en la
  prosa debe igualar la fuente; tolerante a separadores LaTeX `27{,}611`).
- **`tools/check_consistency.py`** (`make consistency`) — escanea los artefactos y **falla
  (exit 1)** ante cualquier violación, indicando archivo:línea y el motivo.

## Comandos

```bash
make key-facts     # regenera la fuente de verdad desde los datos
make consistency   # verifica que TODO esté alineado (incluye el repo web si está como hermano)
make check         # validate + consistency + lint + typecheck + test
```

## Dónde se aplica

- **CI** (`ci.yml`, job `consistency`): en cada push/PR; hace checkout best-effort del repo
  web (`UACJ-MIAAD/VisaPredictAI_WEB`) para chequeo cross-repo (si es inaccesible, valida solo
  el repo de datos y avisa). Variable `VP_WEB_DIR` reubica el repo web.
- **Action del boletín** (`freeze_and_rebuild.yml`): tras un boletín nuevo regenera
  `key_facts.json` con las cifras frescas, de modo que el guardián siga siendo significativo.

## Al cambiar una cifra (el flujo correcto)

1. Cambia el dato/modelo → `make key-facts` (la fuente de verdad se actualiza).
2. `make consistency` → te dice exactamente qué artefactos quedaron desalineados.
3. Reconcilia cada uno al valor canónico (web, `.tex`, paper, README, docs).
4. Repite hasta `✓ Consistencia OK`.

⚠️ **Limitación honesta:** las cifras en la prosa del web/`.tex` están *hardcodeadas* (no se
templatean aún de `key_facts`). El guardián las **detecta** cuando derivan, pero la
reconciliación es manual. Migrar la prosa volátil a las macros `\factXxx` (deliverable) y a
un render desde `key_facts` (web) es trabajo futuro para cerrar el lazo por completo.

## Añadir una regla

Edita `tools/consistency_rules.yml`. Para un claim viejo que no debe volver: una `forbidden`
con su `pattern` y `reason`. Para una cifra que debe coincidir: un `numeric` con un `label`
específico (evita labels ambiguos: "194 series" vale, pero "25 series" también existe y es
legítimo — usa contexto). `{fact}` se sustituye por el valor de `key_facts.json`.
