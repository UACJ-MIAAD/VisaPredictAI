# Paper MICAI 2027 — paquete LaTeX

> **Borrador** del artículo para MICAI (Springer LNCS), derivado de la auditoría
> adversarial de 5 revisores. Números **honestos** ya verificados contra el pipeline.
> El cuerpo es un draft completo y defendible; la redacción final se pule con el asesor.

## Contenido

```
reports/paper_micai/
├── paper.tex              # artículo LNCS doble-ciego (compila: 8 pp.)
├── llncs.cls              # clase oficial Springer (NO modificar)
├── splncs04.bst           # estilo de citas Springer (reservado; el draft usa bib inline)
├── Figures/               # PDFs vectoriales generados (no editar a mano)
│   ├── fig_prospective_horizon.pdf   # MAE+MASE vs horizonte (figura de la contribución)
│   ├── fig_calibration.pdf           # cobertura empírica 80/95 vs horizonte
│   └── fig_fanchart.pdf              # fan-chart de una serie piloto
├── make_paper_figures.py  # regenera Figures/ desde el pipeline (datos reales)
└── README.md
```

## Compilar (Overleaf)

1. Sube `paper.tex`, `llncs.cls` y la carpeta `Figures/`.
2. Compilador = **pdfLaTeX**; corre 2 pasadas (bibliografía **inline**, sin BibTeX).
3. No cargues `geometry` ni cambies márgenes (Springer rechaza desviaciones de `llncs.cls`).

## Regenerar las figuras

```bash
make paper-figures        # = ante/bin/python reports/paper_micai/make_paper_figures.py
```
Lee `reports/forecast_scorecard_meta.json` (evaluación prospectiva),
`reports/web_forecasts.csv` y `data/processed/visa_panel_long.csv`. Cero placeholders;
si los datos cambian, las figuras se actualizan. Estilo LNCS B/N-safe (líneas/marcadores
distintos, no solo color), PDF vectorial.

## Defensas de revisor ya incorporadas

(ver el análisis completo en `docs/PAPER_PROSPECTIVE_DRAFT.md` y la auditoría)

- **Reencuadre como paper de aplicación** (dataset + protocolo prospectivo), no de métodos.
- **Números honestos:** hold-out MASE 0.117 marcado como *señal de selección, no precisión live*;
  prospectivo MASE 0.345 / MAE 146 d como titular; gap 3× **declarado**.
- **cov95 = 0.92 < nominal** reportado como under-coverage honesta; banda 80 % calibrada en
  split disjunto (cov80 held-out 0.81), no circular.
- **FAD = empate, no victoria**; deep gana solo DFF (~15 %); resultado *depende del régimen*.
- **`\paragraph{Leakage prevention}`** (ventana expansible, lags pre-origen, rollout recursivo,
  escala MASE pre-origen, congelado en fecha de origen) + máscara F-only explícita.
- **√h = heurístico** (sin garantía multi-paso); cobertura empírica reportada, no "garantizada".
- **194 estructurales / 74 evaluables**; "deployed" → "evaluated"; demo RAG = artefacto, no contribución.
- **Ética/disponibilidad** (datos públicos agregados, sin IRB, no asesoría legal) + **apéndice de
  reproducibilidad** (versiones pin-eadas py3.14, semillas, walk-forward, provenance = scorecard+git SHA).
- **Amenazas de validez** declaradas: √h, n efectivo (3 añadas), pseudo-replicación RoW,
  dependencia hardware/semilla, supervivencia.

## Pendiente para la sesión con el asesor (no son cambios de código)

- [ ] Tabla numérica **DM/Holm + MCS** y **Friedman–Nemenyi** para el benchmark de 21 modelos
      (los datos están en `reports/`; falta volcarlos a una tabla del `.tex`).
- [ ] Re-implementar **Auto-ARIMA/SARIMA afinado** como fila de baseline explícita bajo el mismo
      walk-forward (pre-empta "venciste strawmen").
- [ ] Rellenar el bloque de autor camera-ready + URLs de repo/sitio (hoy comentados para doble-ciego).
- [ ] Pulir los 2 overfull hboxes menores y la prosa con el Dr. Chente.
