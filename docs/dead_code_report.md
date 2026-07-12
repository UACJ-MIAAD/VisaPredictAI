# Reporte de código muerto — US D1 (plan auditoría 2026-07-12)

Candidatos a código muerto en `vp_model/` con prueba de consumidor (validador adversarial
2026-07-12 + re-verificación con `rg` sobre TODO el repo: código, docs/, reports/latex/,
`.sh`, workflows). Anti-resurrección: `tests/test_public_api.py`.

## Política

- **Borrar exige**: (1) grep exhaustivo cross-repo (código + docs + scripts + CI) con cero
  consumidores fuera de la propia definición, y (2) test anti-resurrección que falle si el
  símbolo reaparece sin consumidor real y sin actualizar este reporte.
- **Un consumidor documental cuenta como consumidor**: `global_summary` se conserva porque
  las guías GPU la citan como snippet copy-paste; su firma queda vigilada por un test que
  parsea la llamada de los `.md` y la compara con `inspect.signature`.

## Disposiciones

| Símbolo | Evidencia (rg exhaustivo 2026-07-12) | Disposición | Commit |
|---|---|---|---|
| `vp_model/ensemble.py::selection_table` | única aparición = su `def`; cero consumidores | **BORRADA** | US D1 (este cambio; el orquestador agrupa el commit) |
| `vp_model/palette.py::country_color` | única aparición = su `def`; su lógica vive vía el dict `COUNTRY` (que sí se importa en los `make_*.py`) | **BORRADA** | US D1 |
| `vp_model/report.py::feature_tables_latex` | única aparición = su `def`; las tablas de caracterización del `.tex` salen de `experiments/make_tex_tables.py` | **BORRADA** (junto con `_CC`, dict auxiliar que solo ella consumía) | US D1 |
| `vp_model/series_characterization.py::advanced_table` | única aparición = su `def`; `advanced()` y `feature_table()` siguen vivos | **BORRADA** | US D1 |
| `vp_model/eval_neuralforecast.py::global_summary` | consumidor documental: `aws_gpu/README.md:48-49` y `aws_gpu/GUIA_EC2.md:129-130` (snippet copy-paste del ranking frontier) | **SE CONSERVA** — humo real + test de firma contra el snippet (`test_public_api.py`) | n/a |

## Verificación

- `rg -n "selection_table|country_color|feature_tables_latex|advanced_table"` → 0 ocurrencias
  post-borrado (incluyendo docs/ y reports/latex/).
- `tests/test_public_api.py`: (a) importa los 5 módulos y asserta que los símbolos borrados
  NO existen; (b) humo de `global_summary` sobre fixture mínima con el esquema de
  `eval_global_deep`; (c) el snippet de las guías GPU se parsea y se bindea contra las
  firmas actuales.
- `ruff` + `mypy` + suite completa verdes (`make check`).
