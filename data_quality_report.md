# Auditoría de calidad de datos — VisaPredict AI

_Generado por `audit_data_quality.py` sobre los CSV vigentes en `data/`._

Convenciones de las columnas: `final_action_dates` = fecha de prioridad publicada; `C` se convirtió a la fecha del boletín y `U` a `NaN` (**el estado original C/F/U no se conserva** — ver hallazgo H1).

## Bloque: Empleo

| País | Filas | Rango | Meses esp. | Faltantes | Dup. clave | Niveles | NaN fecha | DFF |
|---|---|---|---|---|---|---|---|---|
| mexico | 3124 | 2003-10→2026-06 | 273 | 26 | 2 | 16 cats | 3% | ✓ |
| india | 3124 | 2003-10→2026-06 | 273 | 26 | 2 | 16 cats | 3% | ✓ |
| china | 3021 | 2005-04→2026-06 | 255 | 21 | 2 | 16 cats | 3% | ✓ |
| philippines | 3108 | 2003-10→2026-06 | 273 | 28 | 2 | 16 cats | 3% | ✓ |
| row | 1181 | 2016-04→2026-06 | 123 | 0 | 1 | 12 cats | 6% | ✓ |

## Bloque: Familiar

| País | Filas | Rango | Meses esp. | Faltantes | Dup. clave | Niveles | NaN fecha | DFF |
|---|---|---|---|---|---|---|---|---|
| mexico | 1697 | 2006-06→2026-06 | 241 | 30 | 0 | 1/2A/2B/3/4 | 0% | ✓ |
| india | 1697 | 2006-06→2026-06 | 241 | 30 | 0 | 1/2A/2B/3/4 | 0% | ✓ |
| china | 1697 | 2006-06→2026-06 | 241 | 30 | 0 | 1/2A/2B/3/4 | 0% | ✓ |
| philippines | 1692 | 2006-06→2026-06 | 241 | 31 | 0 | 1/2A/2B/3/4 | 0% | ✓ |
| row | 1230 | 2015-05→2026-06 | 134 | 9 | 0 | 1/2A/2B/3/4 | 0% | ✓ |
|   ↳ huecos row: 2015-06, 2015-07, 2015-08, 2015-09, 2015-10, 2015-11, 2015-12, 2016-01, 2016-02 | | | | | | | | |

## Panel consolidado `visa_panel_long.csv`

- Filas: **21,562** · series país×categoría×tabla: **186**
- Status: F=12,859, C=8,242, U=460
- Bloque×tabla: employment/DFF=4,460, employment/FAD=9,089, family/DFF=3,190, family/FAD=4,823
- Objetivo entrenable (status=F): **12,859** filas (60%)
- `days_since_base` ∈ [1400, 16854] (base 1980-01-01); 0 negativos.

## Hallazgos transversales

- **H1 — Estado e∈{C,F,U} ✅ RESUELTO.** Los scrapers ahora emiten las columnas `status` (C/F/U/NA) y `raw_value`; el panel entrena *solo sobre status='F'* y conserva C/U como anotación descriptiva (formulación v5.1).
- **H2 — DFF de Empleo ✅ RESUELTO.** El scraper de empleo ahora captura las dos tablas (FAD + DFF, vía `table_type`); DFF disponible desde Oct-2015. +2,032 filas DFF de empleo, +20 series.
- **H3 — EB-5 y subcategorías ✅ RESUELTO.** `classify_eb_category()` mapea las etiquetas (con 20 años de deriva) a 16 códigos canónicos: EB1-4, EB3_OW, EB4_RW/TRANS, y EB5 (bare/TEA/PILOT/RC/NONRC/UNRESERVED/RURAL/HIGHUNEMP/INFRA). Schedule A queda fuera de alcance. Panel 90→186 series.
- **H4 — FAD no llega a 1992.** El acordeón de travel.state.gov no lista boletines pre-2003; el histórico 1996–2002 vive en páginas archivadas. *Pendiente.*
- **H5 — `NaN` ambiguo ✅ RESUELTO.** `status` distingue 'U' (Unavailable) de 'NA' (celda vacía/no parseable). En el panel actual: 0 filas NA.
