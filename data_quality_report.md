# Auditoría de calidad de datos — VisaPredict AI

_Generado por `audit_data_quality.py` sobre los CSV vigentes en `data/`._

Convenciones de las columnas: `final_action_dates` = fecha de prioridad publicada; `C` se convirtió a la fecha del boletín y `U` a `NaN` (**el estado original C/F/U no se conserva** — ver hallazgo H1).

## Bloque: Empleo

| País | Filas | Rango | Meses esp. | Faltantes | Dup. clave | Niveles | NaN fecha | DFF |
|---|---|---|---|---|---|---|---|---|
| mexico | 3450 | 2001-12→2026-06 | 295 | 14 | 0 | 16 cats | 3% | ✓ |
|   ↳ huecos mexico: 2005-01, 2005-02, 2005-03, 2005-07, 2005-09, 2005-10, 2005-11, 2005-12, 2006-01, 2009-03, 2009-09, 2009-10, 2009-11, 2012-10 | | | | | | | | |
| india | 3395 | 2001-12→2026-06 | 295 | 21 | 0 | 16 cats | 3% | ✓ |
| china | 3157 | 2005-04→2026-06 | 255 | 11 | 0 | 16 cats | 3% | ✓ |
|   ↳ huecos china: 2005-07, 2005-09, 2005-10, 2005-11, 2005-12, 2006-01, 2009-03, 2009-09, 2009-10, 2009-11, 2012-10 | | | | | | | | |
| philippines | 3434 | 2001-12→2026-06 | 295 | 16 | 0 | 16 cats | 3% | ✓ |
|   ↳ huecos philippines: 2004-07, 2005-01, 2005-02, 2005-03, 2005-07, 2005-09, 2005-10, 2005-11, 2005-12, 2006-01, 2006-02, 2009-03, 2009-09, 2009-10, 2009-11, 2012-10 | | | | | | | | |
| row | 3499 | 2001-12→2026-06 | 295 | 7 | 0 | 16 cats | 3% | ✓ |
|   ↳ huecos row: 2005-04, 2009-03, 2009-07, 2009-09, 2009-10, 2009-11, 2012-10 | | | | | | | | |

## Bloque: Familiar

| País | Filas | Rango | Meses esp. | Faltantes | Dup. clave | Niveles | NaN fecha | DFF |
|---|---|---|---|---|---|---|---|---|
| mexico | 2094 | 2001-12→2026-06 | 295 | 5 | 0 | 1/2A/2B/3/4 | 1% | ✓ |
|   ↳ huecos mexico: 2009-03, 2009-09, 2009-10, 2009-11, 2012-10 | | | | | | | | |
| india | 2040 | 2001-12→2026-06 | 295 | 16 | 0 | 1/2A/2B/3/4 | 0% | ✓ |
|   ↳ huecos india: 2002-08, 2002-09, 2002-10, 2002-11, 2002-12, 2003-01, 2003-02, 2003-03, 2003-04, 2003-05, 2003-06, 2009-03, 2009-09, 2009-10, 2009-11, 2012-10 | | | | | | | | |
| china | 1910 | 2005-01→2026-06 | 258 | 5 | 0 | 1/2A/2B/3/4 | 0% | ✓ |
|   ↳ huecos china: 2009-03, 2009-09, 2009-10, 2009-11, 2012-10 | | | | | | | | |
| philippines | 2084 | 2001-12→2026-06 | 295 | 7 | 0 | 1/2A/2B/3/4 | 0% | ✓ |
|   ↳ huecos philippines: 2006-03, 2009-03, 2009-06, 2009-09, 2009-10, 2009-11, 2012-10 | | | | | | | | |
| row | 2064 | 2001-12→2026-06 | 295 | 9 | 0 | 1/2A/2B/3/4 | 0% | ✓ |
|   ↳ huecos row: 2005-04, 2005-05, 2009-03, 2009-07, 2009-09, 2009-10, 2009-11, 2012-10, 2014-11 | | | | | | | | |

## Panel consolidado `visa_panel_long.csv`

- Filas: **27,127** · series país×categoría×tabla: **194**
- Status: F=15,662, C=10,896, U=568, UNK=1
- Bloque×tabla: employment/DFF=5,648, employment/FAD=11,287, family/DFF=3,225, family/FAD=6,967
- Objetivo entrenable (status=F): **15,662** filas (58%)
- `days_since_base` ∈ [1765, 18680] (base 1975-01-01); 0 negativos.

## Hallazgos transversales

- **H1 — Estado e∈{C,F,U} ✅ RESUELTO.** Los scrapers ahora emiten las columnas `status` (C/F/U/UNK) y `raw_value`; el panel entrena *solo sobre status='F'* y conserva C/U como anotación descriptiva (formulación v5.1).
- **H2 — DFF de Empleo ✅ RESUELTO.** El scraper de empleo ahora captura las dos tablas (FAD + DFF, vía `table_type`); DFF disponible desde Oct-2015. +2,032 filas DFF de empleo, +20 series.
- **H3 — EB-5 y subcategorías ✅ RESUELTO.** `classify_eb_category()` mapea las etiquetas (con 20 años de deriva) a 16 códigos canónicos: EB1-4, EB3_OW, EB4_RW/TRANS, y EB5 (bare/TEA/PILOT/RC/NONRC/UNRESERVED/RURAL/HIGHUNEMP/INFRA). Schedule A queda fuera de alcance. Panel 90→186 series.
- **H4 — Cobertura extendida al piso de la fuente ✅ (parcial).** Detección robusta de columnas/sección en **ambos scrapers** (categoría = col 0; sección por `employment[\s-]*based` / substring `family`; RoW por `except those listed`) recuperó **2001-12→2003-09**, el **cluster 2007-2008** y **arregló RoW** (empleo truncado a 2016-04, familiar a 2015-05). Huecos familiares 58-69→6-17. **Piso real = dic-2001**: pre-2002 da 404 en travel.state.gov; llegar a 1992 exigiría Wayback Machine (fuera de alcance). ⚠️ El `.tex` afirma 'FAD desde 1992 (~408 obs)' — irreal desde la fuente oficial (~294 meses máx).
- **H5 — `NaN` ambiguo ✅ RESUELTO.** `status` distingue 'U' (Unavailable) de 'UNK' (celda vacía/no parseable). Centinela `UNK` (no `NA`) para evitar la coerción a NaN de pandas. En el panel actual: 1 fila UNK.
