# 📊 VisaBulletinScraping

Herramienta de web scraping para extraer datos históricos del [Visa Bulletin](https://travel.state.gov/content/travel/en/legal/visa-law0/visa-bulletin.html) del Departamento de Estado de EE.UU. Este repositorio forma parte del proyecto de tesis **VisaPredict AI**, que busca predecir fechas de boletines de visa de inmigración mediante Machine Learning.

## 🎯 Objetivo

Recopilar y estructurar los datos históricos del Visa Bulletin (disponibles desde 1982) para su uso en modelos predictivos de series de tiempo. El scraping extrae las **Priority Dates** publicadas mensualmente, que determinan cuándo un solicitante puede avanzar en su proceso migratorio.

## 📋 ¿Qué es el Visa Bulletin?

El Visa Bulletin es un boletín mensual publicado por el Bureau of Consular Affairs del Departamento de Estado de EE.UU. Contiene dos tablas principales por categoría:

- **Tabla A — Final Action Dates:** Fecha a partir de la cual una visa puede ser emitida o se puede adjudicar el ajuste de estatus.
- **Tabla B — Dates for Filing:** Fecha a partir de la cual un solicitante puede presentar su aplicación.

Cada tabla reporta fechas de prioridad para dos tipos de categorías:

### Family-Sponsored (Patrocinio Familiar)
| Categoría | Descripción |
|-----------|-------------|
| F1 | Hijos solteros adultos de ciudadanos estadounidenses |
| F2A | Cónyuges e hijos menores de residentes permanentes |
| F2B | Hijos solteros adultos (21+) de residentes permanentes |
| F3 | Hijos casados de ciudadanos estadounidenses |
| F4 | Hermanos de ciudadanos estadounidenses adultos |

### Employment-Based (Basado en Empleo)
| Categoría | Descripción |
|-----------|-------------|
| EB-1 | Trabajadores con prioridad (habilidades extraordinarias) |
| EB-2 | Profesionales con grado avanzado |
| EB-3 | Trabajadores calificados y profesionales |
| EB-4 | Inmigrantes especiales |
| EB-5 | Inversionistas |

### Países con límites especiales
Debido a la alta demanda, algunos países tienen fechas de prioridad separadas: **China (mainland)**, **India**, **México** y **Filipinas**. El resto se agrupa como **ROW** (Rest of World).

## 🗂️ Estructura del Repositorio

```
VisaBulletinScraping/
├── scrape_visa_bulletins.py            # Scraper para categorías Employment-Based
├── scrape_family_visa_bulletins.py     # Scraper para categorías Family-Sponsored
├── visualize_visa_wait_times.py        # Gráficas para Employment-Based
├── visualize_family_wait_times.py      # Gráficas para Family-Sponsored
├── requirements.txt                    # Dependencias de Python
├── CLAUDE.md                           # Contexto para Claude Code
├── data/                               # CSVs generados por los scrapers
│   ├── {country}_visa_backlog_timecourse.csv          # Datos EB
│   └── {country}_family_visa_backlog_timecourse.csv   # Datos Family
├── figures/                            # Gráficas generadas
└── ante/                               # Ambiente virtual Python
```

## ⚙️ Requisitos

- Python 3.10+
- macOS / Linux / Windows

### Dependencias
```
pandas>=2.2.2
matplotlib>=3.9.0
beautifulsoup4>=4.12.2
requests>=2.31.0
tqdm>=4.66.1
```

## 🚀 Instalación y Uso

```bash
# 1. Clonar el repositorio
git clone https://github.com/tu-usuario/VisaBulletinScraping.git
cd VisaBulletinScraping

# 2. Crear y activar ambiente virtual
python -m venv ante
source ante/bin/activate        # macOS/Linux
# ante\Scripts\activate         # Windows

# 3. Instalar dependencias
pip install -r requirements.txt

# 4. Ejecutar scrapers
python scrape_visa_bulletins.py           # Employment-Based (~2 min)
python scrape_family_visa_bulletins.py    # Family-Sponsored (~2 min)

# 5. Generar visualizaciones
python visualize_visa_wait_times.py
python visualize_family_wait_times.py
```

## 📊 Datos de Salida

### CSVs Employment-Based
| Columna | Descripción |
|---------|-------------|
| `EB_level` | Categoría (1, 2, 3, 4, 5) |
| `final_action_dates` | Fecha de acción final publicada |
| `visa_bulletin_date` | Fecha del boletín mensual |
| `visa_wait_time` | Tiempo de espera calculado (días) |

### CSVs Family-Sponsored
| Columna | Descripción |
|---------|-------------|
| `F_level` | Categoría (1, 2A, 2B, 3, 4) |
| `final_action_dates` | Fecha de acción final publicada |
| `visa_bulletin_date` | Fecha del boletín mensual |
| `visa_wait_time` | Tiempo de espera calculado (días) |
| `table_type` | `final_action` (Tabla A) o `dates_for_filing` (Tabla B) |

### Valores Especiales
- **C (Current):** La categoría está al día; `wait_time = 0`
- **U (Unavailable):** No hay visas disponibles; `wait_time = NaN`

## 📈 Visualizaciones

Los scripts de visualización generan gráficas por país en `figures/`, mostrando la evolución histórica de los tiempos de espera por categoría de preferencia.

## 🔗 Fuente de Datos

Todos los datos se extraen directamente del sitio oficial del Departamento de Estado:
- **URL:** https://travel.state.gov/content/travel/en/legal/visa-law0/visa-bulletin.html
- **Formato de fechas:** DD-MMM-YY (día-mes-año)
- **Años fiscales disponibles:** Desde 1982 hasta el presente

## 🎓 Contexto Académico

Este repositorio es el componente de adquisición de datos del proyecto de tesis **"VisaPredict AI"**, desarrollado como parte de la Maestría en Inteligencia Artificial y Analítica de Datos (MIAAD) en la Universidad Autónoma de Ciudad Juárez (UACJ).

- **Autor:** Javier Rebull
- **Asesor:** Dr. Vicente García Jiménez
- **Programa:** MIAAD — UACJ

## 📄 Licencia

Este proyecto es para fines académicos y de investigación.

## 🙏 Créditos

Basado en el repositorio original [visa_dates](https://github.com/DavidBellamy/visa_dates) de David Bellamy, extendido con soporte para categorías Family-Sponsored y extracción de ambas tablas (A y B).
