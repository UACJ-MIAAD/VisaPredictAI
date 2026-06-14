# Auditoría de calidad de datos — VisaPredict AI

_Generado por `audit_data_quality.py` sobre los CSV vigentes en `data/`._

Convenciones de las columnas: `final_action_dates` = fecha de prioridad publicada; `C` se convirtió a la fecha del boletín y `U` a `NaN` (**el estado original C/F/U no se conserva** — ver hallazgo H1).

## Bloque: Empleo

| País | Filas | Rango | Meses esp. | Faltantes | Dup. clave | Niveles | NaN fecha | DFF |
|---|---|---|---|---|---|---|---|---|
| mexico | 980 | 2003-10→2026-06 | 273 | 28 | 0 | 1/2/3/4 | 2% | ✗ (no extrae) |
| india | 980 | 2003-10→2026-06 | 273 | 28 | 0 | 1/2/3/4 | 2% | ✗ (no extrae) |
| china | 928 | 2005-04→2026-06 | 255 | 23 | 0 | 1/2/3/4 | 2% | ✗ (no extrae) |
| philippines | 972 | 2003-10→2026-06 | 273 | 30 | 0 | 1/2/3/4 | 2% | ✗ (no extrae) |
| row | 492 | 2016-04→2026-06 | 123 | 0 | 0 | 1/2/3/4 | 1% | ✗ (no extrae) |

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

- Filas: **12,365** · series país×categoría×tabla: **70**
- Status: F=9,645, C=2,632, U=88
- Bloque×tabla: employment/FAD=4,352, family/DFF=3,190, family/FAD=4,823
- Objetivo entrenable (status=F): **9,645** filas (78%)
- `days_since_base` ∈ [1400, 16854] (base 1980-01-01); 0 negativos.

## Hallazgos transversales

- **H1 — Estado e∈{C,F,U} ✅ RESUELTO.** Los scrapers ahora emiten las columnas `status` (C/F/U/NA) y `raw_value`; el panel entrena *solo sobre status='F'* y conserva C/U como anotación descriptiva (formulación v5.1).
- **H2 — DFF ausente en Empleo.** El scraper de empleo corta tras la primera tabla (solo FAD). *Pendiente.*
- **H3 — EB-5 y subcategorías descartadas.** Filtro deja solo EB 1–4. *Pendiente.*
- **H4 — FAD no llega a 1992.** El acordeón de travel.state.gov no lista boletines pre-2003; el histórico 1996–2002 vive en páginas archivadas. *Pendiente.*
- **H5 — `NaN` ambiguo ✅ RESUELTO.** `status` distingue 'U' (Unavailable) de 'NA' (celda vacía/no parseable). En el panel actual: 0 filas NA.
