# CLAUDE.md — VisaPredict AI

## Proyecto y contexto de tesis

Este repositorio extrae datos históricos del **Visa Bulletin** del Departamento de Estado de EE.UU. mediante web scraping. Sirve como la base de datos para la tesis **"VisaPredict AI"** — predicción de fechas de boletines de visa con Machine Learning.

- **Autor:** Sly (Haowei)
- **Programa:** Maestría en Inteligencia Artificial y Analítica de Datos (MIAAD), UACJ
- **Asesor:** Dr. Vicente García Jiménez
- **Repo base (fork):** https://github.com/DavidBellamy/visa_dates
- **Fuente de datos:** https://travel.state.gov/content/travel/en/legal/visa-law0/visa-bulletin.html

## Estructura del repositorio

```
visa_dates/
├── CLAUDE.md                          # Este archivo (contexto para Claude Code)
├── README.md                          # Documentación original del repo
├── requirements.txt                   # Dependencias Python
├── scrape_visa_bulletins.py           # Script principal de scraping (~2 min de ejecución)
├── visualize_visa_wait_times.py       # Generación de gráficas por país
├── data/                              # CSVs generados por el scraper
│   ├── china_visa_backlog_timecourse.csv
│   ├── india_visa_backlog_timecourse.csv
│   ├── mexico_visa_backlog_timecourse.csv
│   ├── philippines_visa_backlog_timecourse.csv
│   └── row_visa_backlog_timecourse.csv
├── figures/                           # Gráficas PNG generadas
│   ├── China_visa_wait_times.png
│   ├── India_visa_wait_times.png
│   ├── Mexico_visa_wait_times.png
│   ├── Philippines_visa_wait_times.png
│   └── RoW_visa_wait_times.png
├── ante/                              # Ambiente virtual Python (no versionar)
└── .github/workflows/
    └── update_graphs.yml              # GitHub Action: scraping diario a medianoche
```

## Stack técnico

- **Python:** 3.14 (macOS Apple Silicon)
- **Ambiente virtual:** `ante` (activar con `source ante/bin/activate`)
- **Ubicación local:** `/Users/haowei/Documents/Anteproyecto/visa_dates`

### Dependencias (requirements.txt)

| Paquete         | Uso                                        |
|-----------------|---------------------------------------------|
| beautifulsoup4  | Parsing HTML del Visa Bulletin              |
| requests        | HTTP requests a travel.state.gov            |
| pandas          | Manipulación de DataFrames y CSVs           |
| matplotlib      | Generación de gráficas                      |
| tqdm            | Barras de progreso durante el scraping      |

## Comandos clave

```bash
# Activar ambiente virtual
source ante/bin/activate

# Instalar dependencias
pip install -r requirements.txt

# Ejecutar scraper (genera CSVs en data/, ~2 min)
python scrape_visa_bulletins.py

# Generar gráficas (genera PNGs en figures/)
python visualize_visa_wait_times.py
```

## Contexto del scraping

El scraper extrae tablas **Employment-Based** del Visa Bulletin mensual publicado por el Departamento de Estado de EE.UU.

### Qué extrae

- **Tabla:** Final Action Dates (primera tabla employment-based de cada boletín mensual)
- **Categorías:** EB-1, EB-2, EB-3, EB-4 (niveles de visa basados en empleo)
- **Países con límites especiales:** India, China, México, Filipinas
- **Resto del mundo (RoW):** "All chargeability areas except those listed"
- **Rango temporal:** Desde Oct 2007 hasta el boletín más reciente

### Estructura de los CSVs

| Columna              | Tipo     | Descripción                                           |
|----------------------|----------|-------------------------------------------------------|
| `EB_level`           | int (1-4)| Nivel de visa employment-based                        |
| `final_action_dates` | datetime | Fecha de acción final publicada en el boletín         |
| `visa_bulletin_date` | datetime | Fecha del boletín mensual                             |
| `visa_wait_time`     | float    | Tiempo de espera calculado en **años** (bulletin_date - final_action_date) |

### Valores especiales

- `C` (Current): sin backlog, se convierte a la fecha del boletín (wait_time = 0)
- `U` (Unavailable): sin visas disponibles, se convierte a `NaN`

## Objetivo: VisaPredict AI

Construir modelos predictivos para forecasting de fechas del Visa Bulletin, con enfoque principal en **México**:

- **Modelos planeados:** XGBoost, modelos de series de tiempo (ARIMA, Prophet, LSTM)
- **Variable objetivo:** `final_action_dates` o `visa_wait_time` futuras
- **Granularidad:** por país y nivel EB
- **Enfoque geográfico:** México (principal), con comparativas contra India, China, Filipinas y RoW

## Convenciones

- **Código:** en inglés (variables, funciones, comentarios técnicos)
- **Documentación académica:** en español, LaTeX/Overleaf
- **Datos crudos:** solo generados vía scripts (nunca editar CSVs manualmente)
- **Gráficos:** publication-ready para la tesis
- **Estilo:** PEP 8
- **Git:** no versionar el ambiente virtual `ante/`

## Notas para Claude Code

- Los CSVs en `data/` son **generados automáticamente** por `scrape_visa_bulletins.py`. No editarlos a mano; si necesitan cambios, modificar el scraper.
- El scraper tarda ~2 minutos en ejecutarse porque hace requests HTTP a cada boletín mensual individualmente.
- Los `NaN` en `visa_wait_time` corresponden a meses donde la categoría estaba `U` (Unavailable).
- El GitHub Action (`update_graphs.yml`) corre diariamente y auto-commitea si hay cambios.
- Al agregar nuevas dependencias, actualizar `requirements.txt`.
- Para análisis exploratorio o modelado, crear scripts/notebooks nuevos en la raíz o en un directorio dedicado (ej. `notebooks/`, `models/`).
- El repo original es de David Bellamy; este fork es la base de datos para la tesis de Sly.
