<p align="center">
  <img src="https://upload.wikimedia.org/wikipedia/commons/thumb/2/28/Escudo_UACJ.svg/500px-Escudo_UACJ.svg.png" alt="UACJ" width="150">
</p>

<h1 align="center" style="color:#003CA6;">VisaBulletinScraping</h1>

<p align="center">
  <strong>Maestría en Inteligencia Artificial y Analítica de Datos (MIAAD)</strong><br>
  Universidad Autónoma de Ciudad Juárez!
</p>

<p align="center">
  <img src="https://img.shields.io/badge/UACJ-003CA6?style=flat-square&logo=data:image/svg+xml;base64,&logoColor=white" alt="UACJ">
  <img src="https://img.shields.io/badge/MIAAD-FFD600?style=flat-square&logoColor=231F20" alt="MIAAD">
  <img src="https://img.shields.io/badge/Python-3.14-555559?style=flat-square&logo=python&logoColor=FFD600" alt="Python">
  <img src="https://img.shields.io/badge/License-Academic-003CA6?style=flat-square" alt="License">
</p>

---

Pipeline de extracción, anotación, consolidación y auditoría de los datos históricos del [Visa Bulletin](https://travel.state.gov/content/travel/en/legal/visa-law0/visa-bulletin.html) del Departamento de Estado de EE.UU. Es el componente de datos (Objetivo 1) del proyecto de tesis **VisaPredict AI**, que busca predecir fechas de prioridad de inmigración mediante Machine Learning.

## Objetivo

Construir un **panel multiserie** $y_{p,c,b,t}$ (país × categoría × tabla × mes) con las fechas de prioridad publicadas, listo para modelado de series de tiempo. El pipeline se ejecuta a diario vía GitHub Action.

- **5 países o áreas de cargabilidad:** México, India, China, Filipinas y *All Chargeability Areas Except Those Listed* (RoW).
- **Categorías:** Family-Sponsored (F1, F2A, F2B, F3, F4) y Employment-Based (EB-1 a EB-5 con subcategorías, 16 códigos canónicos).
- **Dos tablas evaluadas por separado:** *Final Action Dates* (FAD) y *Dates for Filing* (DFF).
- **Cobertura:** serie mensual homogénea desde **diciembre de 2001** hasta el presente (~290 observaciones por serie). Los boletines previos a 2001 existen solo en fuentes de archivo/estadística.

## Qué es el Visa Bulletin

Boletín mensual del Bureau of Consular Affairs con dos tablas por categoría:

- **Tabla A -- Final Action Dates (FAD):** fecha a partir de la cual se puede adjudicar la residencia.
- **Tabla B -- Dates for Filing (DFF):** fecha a partir de la cual se puede iniciar el trámite (disponible desde oct-2015).

## Estructura del repositorio

```
VisaBulletinScraping/
├── visa_common.py                      # helpers compartidos (fetch, parse, estado) — fuente única
├── config.py                           # constantes (países canónicos, epoch, paleta)
├── scrape_visa_bulletins.py            # scraper Employment-Based (FAD + DFF)
├── scrape_family_visa_bulletins.py     # scraper Family-Sponsored (FAD + DFF)
├── build_panel.py                      # consolida los 10 CSV en el panel largo
├── audit_data_quality.py · mega_audit.py   # auditorías de calidad de datos
├── visualize_*.py                      # gráficas (artefactos no versionados)
├── tests/                              # pytest: parsers · extracción offline · contrato del panel
├── data/                               # CSVs por país + visa_panel_long.csv (versionados)
├── Makefile · pyproject.toml           # one-command ops + config ruff/mypy/pytest
└── .github/workflows/                  # ci.yml (lint+type+test) · update_graphs.yml (cron diario)
```

## Requisitos

- Python 3.14 (las dependencias están pin-eadas en `requirements.txt` para reproducibilidad dev↔CI).

## Instalación y uso

```bash
git clone https://github.com/UACJ-MIAAD/VisaBulletinScraping.git
cd VisaBulletinScraping
python -m venv ante && source ante/bin/activate   # ante\Scripts\activate en Windows
make install            # dependencias + herramientas dev

# pipeline de un comando
make scrape             # ambos scrapers (~4 min, red)
make panel              # consolida data/visa_panel_long.csv
make test               # pytest (parsers + extracción offline + contrato del panel)
make check              # ruff + mypy + pytest
make figures            # gráficas (no versionadas)
```

## Datos de salida

### CSVs por país (`data/{country}[_family]_visa_backlog_timecourse.csv`)

| Columna | Descripción |
|---|---|
| `EB_level` / `F_level` | Categoría: empleo = código canónico (`EB1`…`EB5_RURAL`); familiar = `1`, `2A`, `2B`, `3`, `4` |
| `priority_date` | Fecha de prioridad publicada (parseada) |
| `visa_bulletin_date` | Mes del boletín |
| `table_type` | `final_action` (FAD) o `dates_for_filing` (DFF) |
| `raw_value` | Celda original tal cual se publicó (`01MAY16`, `C`, `U`) |
| `status` | Régimen administrativo: `F`/`C`/`U`/`UNK` (ver abajo) |
| `visa_wait_time` | Tiempo de espera calculado (años, legado) |

### Panel consolidado (`data/visa_panel_long.csv`)

Formato largo con la variable dependiente: `country`, `block`, `category`, `table`, `bulletin_date`, `status`, `priority_date`, **`days_since_base`** (días desde 1975-01-01, solo cuando `status='F'`), `raw_value`.

### Estado administrativo (`status`)

- **`F`** -- se publicó una fecha específica (único objetivo predictivo).
- **`C`** -- *Current*, sin backlog ese mes (anotación descriptiva).
- **`U`** -- *Unavailable*, sin números ese mes (anotación descriptiva).
- **`UNK`** -- celda vacía o no parseable.

## Calidad y reproducibilidad

- **Tests** (`pytest`, gate de cobertura) sobre las funciones de parseo, la extracción offline (fixtures HTML) y el contrato del panel.
- **CI** (`ci.yml`): `ruff` (lint + format) + `mypy` + tests en cada push/PR.
- **Action diaria** (`update_graphs.yml`): scrape → panel → gate de tests → commit; abre un issue si falla.
- **Auditorías** programáticas de calidad de datos (`mega_audit.py`, 12 dimensiones).

## Fuente de datos

- **URL:** https://travel.state.gov/content/travel/en/legal/visa-law0/visa-bulletin.html
- **Formato de fechas:** `DD-MMM-YY`
- **Cobertura del pipeline:** serie mensual continua desde diciembre de 2001 hasta el boletín más reciente (98.3 % de los meses; los ausentes existen solo en el archivo legacy de Wayback).

## Contexto académico

Componente de adquisición de datos del proyecto de tesis **"VisaPredict AI"** (MIAAD, UACJ).

| | |
|---|---|
| **Autor** | Javier Rebull |
| **Asesor** | Dr. Vicente García Jiménez |
| **Programa** | MIAAD -- UACJ |

## Licencia

Para fines académicos y de investigación.

---

<p align="center">
  <strong style="color:#003CA6;">Universidad Autónoma de Ciudad Juárez</strong><br>
  <sub style="color:#555559;">Maestría en Inteligencia Artificial y Analítica de Datos</sub>
</p>
