<p align="center">
  <img src="https://www.uacj.mx/comunicacion/img/Logotipos/UACJ%20Horizontal.png" alt="UACJ" width="320">
</p>

<h1 align="center" style="color:#003CA6;">VisaBulletinScraping</h1>

<p align="center">
  <strong>Maestria en Inteligencia Artificial y Analitica de Datos (MIAAD)</strong><br>
  Universidad Autonoma de Ciudad Juarez
</p>

<p align="center">
  <img src="https://img.shields.io/badge/UACJ-003CA6?style=flat-square&logo=data:image/svg+xml;base64,&logoColor=white" alt="UACJ">
  <img src="https://img.shields.io/badge/MIAAD-FFD600?style=flat-square&logoColor=231F20" alt="MIAAD">
  <img src="https://img.shields.io/badge/Python-3.10+-555559?style=flat-square&logo=python&logoColor=FFD600" alt="Python">
  <img src="https://img.shields.io/badge/License-Academic-003CA6?style=flat-square" alt="License">
</p>

---

Herramienta de web scraping para extraer datos historicos del [Visa Bulletin](https://travel.state.gov/content/travel/en/legal/visa-law0/visa-bulletin.html) del Departamento de Estado de EE.UU. Este repositorio forma parte del proyecto de tesis **VisaPredict AI**, que busca predecir fechas de boletines de visa de inmigracion mediante Machine Learning.

## Objetivo

Recopilar y estructurar los datos historicos del Visa Bulletin (disponibles desde 1982) para su uso en modelos predictivos de series de tiempo. El scraping extrae las **Priority Dates** publicadas mensualmente, que determinan cuando un solicitante puede avanzar en su proceso migratorio.

## Que es el Visa Bulletin

El Visa Bulletin es un boletin mensual publicado por el Bureau of Consular Affairs del Departamento de Estado de EE.UU. Contiene dos tablas principales por categoria:

- **Tabla A -- Final Action Dates:** Fecha a partir de la cual una visa puede ser emitida o se puede adjudicar el ajuste de estatus.
- **Tabla B -- Dates for Filing:** Fecha a partir de la cual un solicitante puede presentar su aplicacion.

Cada tabla reporta fechas de prioridad para dos tipos de categorias:

### Family-Sponsored (Patrocinio Familiar)

| Categoria | Descripcion |
|-----------|-------------|
| F1 | Hijos solteros adultos de ciudadanos estadounidenses |
| F2A | Conyuges e hijos menores de residentes permanentes |
| F2B | Hijos solteros adultos (21+) de residentes permanentes |
| F3 | Hijos casados de ciudadanos estadounidenses |
| F4 | Hermanos de ciudadanos estadounidenses adultos |

### Employment-Based (Basado en Empleo)

| Categoria | Descripcion |
|-----------|-------------|
| EB-1 | Trabajadores con prioridad (habilidades extraordinarias) |
| EB-2 | Profesionales con grado avanzado |
| EB-3 | Trabajadores calificados y profesionales |
| EB-4 | Inmigrantes especiales |
| EB-5 | Inversionistas |

### Paises con limites especiales

Debido a la alta demanda, algunos paises tienen fechas de prioridad separadas: **China (mainland)**, **India**, **Mexico** y **Filipinas**. El resto se agrupa como **ROW** (Rest of World).

## Estructura del Repositorio

```
VisaBulletinScraping/
├── scrape_visa_bulletins.py            # Scraper para categorias Employment-Based
├── scrape_family_visa_bulletins.py     # Scraper para categorias Family-Sponsored
├── visualize_visa_wait_times.py        # Graficas para Employment-Based
├── visualize_family_wait_times.py      # Graficas para Family-Sponsored
├── requirements.txt                    # Dependencias de Python
├── data/                               # CSVs generados por los scrapers
│   ├── {country}_visa_backlog_timecourse.csv          # Datos EB
│   └── {country}_family_visa_backlog_timecourse.csv   # Datos Family
└── figures/                            # Graficas generadas
```

## Requisitos

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

## Instalacion y Uso

```bash
# 1. Clonar el repositorio
git clone https://github.com/UACJ-MIAAD/VisaBulletinScraping.git
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

## Datos de Salida

### CSVs Employment-Based

| Columna | Descripcion |
|---------|-------------|
| `EB_level` | Categoria (1, 2, 3, 4, 5) |
| `final_action_dates` | Fecha de accion final publicada |
| `visa_bulletin_date` | Fecha del boletin mensual |
| `visa_wait_time` | Tiempo de espera calculado (dias) |

### CSVs Family-Sponsored

| Columna | Descripcion |
|---------|-------------|
| `F_level` | Categoria (1, 2A, 2B, 3, 4) |
| `final_action_dates` | Fecha de accion final publicada |
| `visa_bulletin_date` | Fecha del boletin mensual |
| `visa_wait_time` | Tiempo de espera calculado (dias) |
| `table_type` | `final_action` (Tabla A) o `dates_for_filing` (Tabla B) |

### Valores Especiales

- **C (Current):** La categoria esta al dia; `wait_time = 0`
- **U (Unavailable):** No hay visas disponibles; `wait_time = NaN`

## Visualizaciones

Los scripts de visualizacion generan graficas por pais en `figures/`, mostrando la evolucion historica de los tiempos de espera por categoria de preferencia.

## Fuente de Datos

Todos los datos se extraen directamente del sitio oficial del Departamento de Estado:

- **URL:** https://travel.state.gov/content/travel/en/legal/visa-law0/visa-bulletin.html
- **Formato de fechas:** DD-MMM-YY (dia-mes-ano)
- **Anos fiscales disponibles:** Desde 1982 hasta el presente

## Contexto Academico

Este repositorio es el componente de adquisicion de datos del proyecto de tesis **"VisaPredict AI"**, desarrollado como parte de la Maestria en Inteligencia Artificial y Analitica de Datos (MIAAD) en la Universidad Autonoma de Ciudad Juarez (UACJ).

| | |
|---|---|
| **Autor** | Javier Rebull |
| **Asesor** | Dr. Vicente Garcia Jimenez |
| **Programa** | MIAAD -- UACJ |

## Licencia

Este proyecto es para fines academicos y de investigacion.

## Creditos

Basado en el repositorio original [visa_dates](https://github.com/DavidBellamy/visa_dates) de David Bellamy, extendido con soporte para categorias Family-Sponsored y extraccion de ambas tablas (A y B).

---

<p align="center">
  <strong style="color:#003CA6;">Universidad Autonoma de Ciudad Juarez</strong><br>
  <sub style="color:#555559;">Maestria en Inteligencia Artificial y Analitica de Datos</sub>
</p>
