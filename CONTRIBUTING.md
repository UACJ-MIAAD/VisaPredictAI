# Cómo contribuir

Gracias por tu interés. Este repositorio es el componente de datos (Objetivo 1)
de la tesis **VisaPredict AI** (MIAAD, UACJ).

## Entorno

```bash
python -m venv ante && source ante/bin/activate   # Python 3.14
make install          # = pip install -e ".[dev]"  (runtime + herramientas dev)
```

## Antes de abrir un PR

Todo cambio debe pasar el gate completo:

```bash
make check            # ruff (lint + format) + mypy + pytest con coverage gate
```

Si tocas el pipeline de datos, regenera y valida:

```bash
make panel            # reconstruye data/processed/visa_panel_long.csv
make audit            # auditorías -> reports/
```

## Convenciones

- **Código** en inglés (variables, funciones, comentarios técnicos); **documentación**
  en español. PEP 8, líneas ≤ 120, formateado con `ruff format`.
- **Dependencias:** fuente única en `pyproject.toml` (`[project.dependencies]` para
  runtime, `[project.optional-dependencies].dev` para herramientas). No reintroducir
  `requirements.txt` ni *pins* duplicados.
- **Datos:** los CSV de `data/raw/` se generan **solo** con los scrapers; no editarlos
  a mano. El panel de `data/processed/` se genera con `build_panel.py`.
- **Commits:** mensajes en inglés, imperativo y descriptivos.
- **CI** (`ci.yml`) debe quedar en verde; el cron (`update_graphs.yml`) aborta el
  commit diario si el gate de tests falla.
