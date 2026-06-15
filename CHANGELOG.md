# Changelog

Todos los cambios notables de este proyecto se documentan aquí.
Formato basado en [Keep a Changelog](https://keepachangelog.com/es/1.1.0/);
el proyecto sigue [Versionado Semántico](https://semver.org/lang/es/).

## [Sin publicar]

### Añadido
- Estructura tipo *cookiecutter-data-science*: `data/raw/` (CSV por país, fuente)
  y `data/processed/` (panel consolidado, derivado); `reports/` para auditorías
  generadas y `docs/` para documentación.
- `LICENSE` MIT; `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`,
  `CHANGELOG.md`, `.editorconfig`.
- Empaquetado instalable (`pip install -e .[dev]`) con `[build-system]`.

### Cambiado
- `pyproject.toml` es la **fuente única** de dependencias (runtime y dev); se
  eliminó `requirements.txt` y se quitaron los *pins* duplicados de Makefile y CI.
- Workflows migrados al runtime Node 24 (`checkout@v5`, `setup-python@v6`,
  `github-script@v8`).

## [1.0.0] — 2026-06-14

### Añadido
- Pipeline de scraping del U.S. Visa Bulletin (empleo + familiar, FAD + DFF) y
  consolidación en el panel multiserie `y_{p,c,b,t}` (**194 series · 27,127 filas**,
  dic-2001 → presente, base = 1975-01-01).
- Columnas `status` (C/F/U/UNK) y `raw_value`; objetivo entrenable solo sobre
  `status='F'`.
- Suite de calidad: `pytest` con *coverage gate*, contrato de integridad del panel,
  auditorías programáticas (`audit_data_quality.py`, `mega_audit.py`).
- MLOps: CI (`ruff` + `mypy` + tests), cron diario de actualización, pre-commit,
  DVC reservado para artefactos de modelado, `Makefile`.
