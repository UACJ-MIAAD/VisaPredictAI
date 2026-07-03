"""DAG de datos: freeze (red) -> scrape (offline) -> panel -> bulletins/db -> audit.
Cada módulo se invoca como script: `python -m pipeline.scrape_all` (desde la raíz)."""
