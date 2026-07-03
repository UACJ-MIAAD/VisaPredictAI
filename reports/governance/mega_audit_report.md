# MEGA AUDIT — VisaPredict AI panel

_Auditoría exhaustiva generada por `mega_audit.py` sobre `data/processed/visa_panel_long.csv` y las 10 fuentes._

## 1. Esquema & dtypes

- Panel columnas: `['country', 'block', 'category', 'table', 'bulletin_date', 'status', 'priority_date', 'days_since_base', 'raw_value']`
- Faltantes vs esperadas: ninguna ✓
- CSVs fuente:
  ✓ todas las fuentes con columnas requeridas

## 2. Completitud a nivel boletín

- Span: **2001-12 → 2026-07** (296 meses)
- Meses con ≥1 fila en el panel: **296** (100.0%)
- Meses sin ninguna fila: ninguno
- Muertos confirmados (404 + Wayback-only): `[]`
- ✓ Sin meses ausentes inexplicados.

## 3. Inventario de series (país × categoría × tabla)

- Total series: **194**
- Filas: **27,611** · filas status=F: **15,931**
- Por bloque×tabla:

| bloque | tabla | series | filas | F |
|---|---|--:|--:|--:|
| employment | DFF | 64 | 5,698 | 2,169 |
| employment | FAD | 80 | 11,548 | 3,753 |
| family | DFF | 25 | 3,250 | 3,145 |
| family | FAD | 25 | 7,115 | 6,864 |

- Series más cortas (n_F menor), candidatas a exclusión:

| país | cat | tabla | n | n_F | rango |
|---|---|---|--:|--:|---|
| mexico | EB5_TEA | DFF | 1 | 0 | 2015-10→2015-10 |
| india | EB5_HIGHUNEMP | FAD | 51 | 0 | 2022-05→2026-07 |
| india | EB5_RURAL | DFF | 51 | 0 | 2022-05→2026-07 |
| mexico | EB5_UNRESERVED | FAD | 51 | 0 | 2022-05→2026-07 |
| mexico | EB5_UNRESERVED | DFF | 51 | 0 | 2022-05→2026-07 |
| mexico | EB5_RURAL | FAD | 51 | 0 | 2022-05→2026-07 |
| mexico | EB5_RURAL | DFF | 51 | 0 | 2022-05→2026-07 |
| mexico | EB5_RC | FAD | 83 | 0 | 2005-08→2022-04 |
| mexico | EB5_RC | DFF | 78 | 0 | 2015-11→2022-04 |
| mexico | EB5_PILOT | FAD | 21 | 0 | 2009-04→2012-10 |
| mexico | EB5_NONRC | FAD | 80 | 0 | 2015-09→2022-04 |
| mexico | EB5_NONRC | DFF | 78 | 0 | 2015-11→2022-04 |

## 4. Distribución de estado e∈{C,F,U,UNK}

| status | filas | % |
|---|--:|--:|
| F | 15,931 | 57.7% |
| C | 11,058 | 40.0% |
| U | 621 | 2.2% |
| UNK | 1 | 0.0% |

- Por bloque×tabla (solo F / total):

| bloque | tabla | F | total | %F |
|---|---|--:|--:|--:|
| employment | DFF | 2,169 | 5,698 | 38% |
| employment | FAD | 3,753 | 11,548 | 32% |
| family | DFF | 3,145 | 3,250 | 97% |
| family | FAD | 6,864 | 7,115 | 96% |

## 6. Unicidad de clave

- Claves (país,bloque,cat,tabla,mes) duplicadas: **0** ✓

## 7. Validez de fechas

- `days_since_base` negativos: **0** ✓
- priority_date fuera de [1975,2026]: **0** ✓
- priority_date > bulletin_date (fecha futura): **0**
- status=C con raw≠'C': 0 · status=U con raw≠'U': 0
- status fuera del dominio {F,C,U,UNK}: **0** ✓
- contrato days_iff_F: F sin fecha/días **0** · no-F con fecha/días **0** ✓

## 8. Coherencia DFF vs FAD (DFF debe ser ≥ avanzada que FAD)

- Pares (mismo país/cat/mes) con FAD y DFF: **5,011**
- Violaciones DFF < FAD: **6** (0.12%)
- _Interpretación: inversiones reales de pocos días publicadas por el Depto. de Estado (los `raw_value` parsean bien), NO errores de parseo._

  | país | bloque | cat | mes | FAD | DFF |
  |---|---|---|---|---|---|
  | all_chargeability | family | F3 | 2018-03 | 2005-12-15 | 2005-12-01 |
  | china | employment | EB3 | 2017-04 | 2014-08-15 | 2014-05-01 |
  | china | employment | EB3 | 2019-08 | 2016-07-01 | 2016-06-01 |
  | china | employment | EB3_OW | 2020-10 | 2008-12-01 | 2008-10-01 |
  | china | family | F3 | 2018-03 | 2005-12-15 | 2005-12-01 |
  | india | family | F3 | 2018-03 | 2005-12-15 | 2005-12-01 |

## 9. Anomalías de salto (Δ priority_date > 8 años en 1 mes)

- Saltos |Δ| > 8 años: **14** (candidatos a error de parseo o retrogresión fuerte)
- _Interpretación: los revisados son reales — transición EB-4 dic-2022 (`22JUN22`) y backlogs México F1/F3 de los 80s (`01JAN81`); el parseo de año de 2 dígitos es correcto (69-99→19xx, 00-68→20xx). El modelo deberá tolerar retrogresiones._

  | país | cat | tabla | mes | Δaños | priority | raw |
  |---|---|---|---|--:|---|---|
  | all_chargeability | EB4 | FAD | 2022-12 | +15.5 | 2022-06-22 | 22JUN22 |
  | all_chargeability | EB4_RW | FAD | 2022-12 | +15.5 | 2022-06-22 | 22JUN22 |
  | china | EB4 | FAD | 2022-12 | +15.5 | 2022-06-22 | 22JUN22 |
  | china | EB4_RW | FAD | 2022-12 | +15.5 | 2022-06-22 | 22JUN22 |
  | philippines | EB4 | FAD | 2022-12 | +15.5 | 2022-06-22 | 22JUN22 |
  | philippines | EB4_RW | FAD | 2022-12 | +15.5 | 2022-06-22 | 22JUN22 |
  | mexico | F3 | FAD | 2006-08 | -12.8 | 1981-01-01 | 01JAN81 |
  | mexico | F1 | FAD | 2005-07 | -11.8 | 1983-01-01 | 01JAN83 |
  | india | EB1 | FAD | 2023-08 | -10.1 | 2012-01-01 | 01JAN12 |
  | mexico | F1 | FAD | 2005-10 | +10.0 | 1993-01-01 | 01JAN93 |
  | china | EB3_OW | FAD | 2014-06 | -9.7 | 2003-01-01 | 01JAN03 |
  | all_chargeability | EB1 | FAD | 2018-08 | +9.3 | 2016-05-01 | 01MAY16 |
  | mexico | EB1 | FAD | 2018-08 | +9.3 | 2016-05-01 | 01MAY16 |
  | philippines | EB1 | FAD | 2018-08 | +9.3 | 2016-05-01 | 01MAY16 |

## 10. Reconciliación fuente ↔ panel

- Filas por status en las 10 fuentes (suma): **{'C': 11058, 'F': 15931, 'U': 621, 'UNK': 1}**
- Filas por status en el panel: **{'F': 15931, 'C': 11058, 'U': 621, 'UNK': 1}**

## 11. Matriz de cobertura categoría × país (nº de tablas con datos)


| categoría | all_chargeability | china | india | mexico | philippines |
|---|---|---|---|---|---|
| EB1 | 2 | 2 | 2 | 2 | 2 |
| EB2 | 2 | 2 | 2 | 2 | 2 |
| EB3 | 2 | 2 | 2 | 2 | 2 |
| EB3_OW | 2 | 2 | 2 | 2 | 2 |
| EB4 | 2 | 2 | 2 | 2 | 2 |
| EB4_RW | 2 | 2 | 2 | 2 | 2 |
| EB4_TRANS | 1 | 1 | 1 | 1 | 1 |
| EB5 | 1 | 1 | 1 | 1 | 1 |
| EB5_HIGHUNEMP | 2 | 2 | 2 | 2 | 2 |
| EB5_INFRA | 2 | 2 | 2 | 2 | 2 |
| EB5_NONRC | 2 | 2 | 2 | 2 | 2 |
| EB5_PILOT | 1 | 1 | 1 | 1 | 1 |
| EB5_RC | 2 | 2 | 2 | 2 | 2 |
| EB5_RURAL | 2 | 2 | 2 | 2 | 2 |
| EB5_TEA | 1 | 2 | 2 | 2 | 2 |
| EB5_UNRESERVED | 2 | 2 | 2 | 2 | 2 |
| F1 | 2 | 2 | 2 | 2 | 2 |
| F2A | 2 | 2 | 2 | 2 | 2 |
| F2B | 2 | 2 | 2 | 2 | 2 |
| F3 | 2 | 2 | 2 | 2 | 2 |
| F4 | 2 | 2 | 2 | 2 | 2 |

## 12. Vista previa de entrenabilidad (F totales y corrida F continua máxima)

- Series con ≥ 24 obs F: **115** / 194 · con corrida F CONTINUA ≥ 24: **106**
- Series con ≥ 60 obs F: **80** / 194 · con corrida F CONTINUA ≥ 60: **71**
- Series con ≥ 120 obs F: **63** / 194 · con corrida F CONTINUA ≥ 120: **49**

- Las series con n_F bajo (EB-5 set-asides, categorías sin columna histórica) son cobertura **estructural**; el filtro evaluable/piloto del anteproyecto las descarta para modelado.

## Veredicto

- 🔴 CRÍTICOS: **0** — ninguno ✓
- 🟡 ADVERTENCIAS: **1**: 1 filas status=UNK (celdas no parseadas)
- 🔵 INFORMATIVOS: **2**: 6 pares con DFF anterior a FAD (revisar); 14 saltos grandes mes-a-mes

**Estado del panel: APTO**
