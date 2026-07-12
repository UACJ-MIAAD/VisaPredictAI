# Política de limpieza de datos — definición central

> **Fuente máquina-legible:** `vp_data/cleaning.py::CLEANING_DECISIONS` (este documento es su
> gemelo en prosa; ante discrepancia gana el código). **Ledger por build:**
> `reports/governance/cleaning_ledger.json`, escrito por `pipeline/build_panel.py` en cada
> consolidación (determinista, versionado en git — el historial de builds es el historial del ledger).

## Las decisiones (qué · dónde se aplica · qué fallo previene)

| # | Decisión | Módulo dueño | Fallo que previene |
|---|---|---|---|
| 1 | **Régimen C/F/U/UNK como anotación; F único objetivo** | `vp_data/visa_common.py::classify_status` | Aplanar C→fecha / U→NaN destruía el régimen; la evaluación enmascara todo lo no-F (B1) |
| 2 | **Centinela `UNK`, jamás `NA`** | `visa_common.py::classify_status` | El string "NA" lo coerciona `read_csv` a NaN y borra la anotación |
| 3 | **Pivote de siglo + guardia de época** | `visa_common.py::string_to_datetime` · `build_panel` (underflow) · `schema.sql::days_is_datediff` | Un '69→1969 silencioso; fechas antes de t0=1975 abortan, no truncan |
| 4 | **Tolerancia a footnotes/erratas sin corregir en silencio** | `visa_common.py` | `C*`/`U*`/'4rd' no descartan el mes; lo imparseable queda UNK con `raw_value` intacto |
| 5 | **Dedup por preferencia F>C>U>UNK; dos F ≠ → aborta** | `pipeline/build_panel.py` | `first` era moneda al aire: podía tirar una observación F entrenable |
| 6 | **Fechas imparseables abortan en la causa** (F `priority_date` **y** `bulletin_date`) | `pipeline/build_panel.py` (AA3) | El NaT viajaba hasta el CHECK del almacén / merge de `dim_date`, lejos del origen |
| 7 | **Dominios de categoría validados post-read** | `build_panel.py::load_*` (AA4) | `keep_default_na=False` desactiva coerción NA de todo el frame; un literal extraño pasaría como string |
| 8 | **Huecos de entrenamiento: relleno CAUSAL (LOCF), jamás puntuado** | `vp_model/preprocess.py::to_regular_monthly_causal` · `models.py::to_timeseries` (AB4/US-F1) | La interpolación lineal bidireccional usaba el bracket FUTURO del hueco (fuga hacia los orígenes dentro del hueco); la evaluación puntúa sólo F reales (máscara B1) y los GBM reciben las máscaras MNAR |
| 9 | **EDA imputa con Kalman, nunca rampa sin tope** | `vp_model/series_characterization.py::_clean` → `missingness.kalman_impute` (AB1) | Tendencia fabricada contaminando STL/Hurst/changepoints/entropía |
| 10 | **Pruebas formales sobre F crudas, con caveat de espaciado** | `experiments/build_eda_facts.py::_formal_tests` (AB3) | Imputar antes de ADF/KPSS sesga hacia "integrada"; el costo (calendario comprimido en huecos) queda documentado |
| 11 | **Outliers = señal: se cuentan, jamás se recortan** | `series_characterization.py::count_outliers` (z-STL) · `n_point_anomalies` (Hampel) · `mega_audit::d9_jumps` (AC2) | Winsorizar borraría las retrogresiones que el modelo debe tolerar |
| 12 | **Contrato re-verificado declarativamente** | `schema.sql` (`days_iff_F`, `pdate_iff_F`, `days_is_datediff`, `rank_iff_F`, PK/FK) | Una fila que viole la invariante revienta EN LA CARGA con el nombre de la invariante |

## Matriz de densificación (una política por ruta, sin contradicciones)

| Ruta | Tratamiento de huecos | Por qué |
|---|---|---|
| **Entrenamiento local** (`models.to_timeseries` → `to_regular_monthly_causal`) | **LOCF causal** (forward-only, sin tope) **solo para continuidad** + máscaras MNAR a los GBM | Los modelos no toleran NaN; el valor de un mes de hueco usa SOLO observaciones anteriores (US-F1) y los puntos fabricados jamás se puntúan (B1) |
| **Evaluación** (todas las rutas) | **Sin imputar**: máscara F-only (`metrics._aligned(dates=)`) | Puntuar meses inventados deprimía el error (bug B1, corregido 2-jul-2026) |
| **Deep global** (`run_global_deep.regular_monthly`) | ≤3 lineal; hueco largo = inicio de serie nueva (segmento contiguo más reciente) | La NN no aprende de rampas sintéticas |
| **Caracterización EDA** (`series_characterization._clean`) | ≤3 lineal + huecos largos por **Kalman** (espacio de estados), sin extrapolar bordes | STL/espectro exigen serie completa; Kalman no fabrica tendencia lineal (AB1) |
| **Censo de estacionariedad** (`build_eda_facts._formal_tests`) | **Sin imputar** (F crudas) | Imputar sesga las pruebas de raíz unitaria; caveat de espaciado documentado (AB3) |
| ~~`run_neuralforecast.py`~~ | interpolaba sin tope — **DEPRECADO** (B7); sólo registro histórico del claim retractado | — |

## Política de outliers (AC2)

Las **retrogresiones** y los saltos |Δ| > 8 años son **eventos administrativos reales**
(agotamiento de cuotas, cambios de régimen), no errores de medición. Por diseño:

1. **Ningún paso del pipeline winsoriza, recorta ni elimina** valores extremos del dato.
2. Los outliers **se cuentan** como característica, con estadísticos robustos:
   z-robusto sobre residuo STL (`count_outliers`, MAD Iglewicz-Hoaglin, |z|>3) y
   filtro de Hampel local (`n_point_anomalies`); los cambios de régimen se separan con
   PELT (`n_changepoints`). MAD=0 (serie casi constante) emite warning, no degrada muda (AA5).
3. `mega_audit::d9_jumps` reporta los saltos >8 años como **INFO** con la interpretación
   "son reales; el modelo debe tolerarlos".
4. Las **figuras** no recortan en silencio: si un histograma muestra un rango central,
   anota cuántos eventos quedan fuera y el extremo (AC1; `plots.plot_step_distribution`,
   `make_fe_figures.fig_differencing`).

## Ledger por build (`cleaning_ledger.json`)

Campos: `vintage` (mes máximo del panel — la clave de versión), `n_rows`, `n_series`,
`rows_by_status`, `dup_collapsed` (filas colapsadas por dedup), guardias documentadas en
cero (`bulletin_date_unparseable`, `f_priority_date_unparseable`, `epoch_underflow` —
un valor >0 es inalcanzable: abortan), `big_jumps_gt_8y` (conteo informativo, señal real).
Sin timestamp de reloj a propósito: la etapa `panel` de DVC exige salida byte-reproducible.
