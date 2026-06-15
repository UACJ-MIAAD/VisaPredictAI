# Política de seguridad

## Alcance

Este proyecto extrae datos **públicos** del U.S. Visa Bulletin
(`travel.state.gov`). No procesa datos personales, no usa credenciales ni claves
de API, y no expone servicios de red. La superficie de seguridad es mínima.

## Reportar una vulnerabilidad

Si encuentras un problema de seguridad (p. ej. ejecución de código a partir de
HTML malicioso parseado, o una dependencia comprometida), repórtalo de forma
privada por correo:

- **Javier Rebull** — `al263483@alumnos.uacj.mx`

Por favor incluye pasos de reproducción y el impacto esperado. No abras un issue
público para vulnerabilidades sin confirmar. Se responderá en un plazo razonable
dado el carácter académico del proyecto.

## Buenas prácticas vigentes

- Dependencias **pin-eadas** en `pyproject.toml` (fuente única).
- CI con `ruff` + `mypy` + tests en cada cambio.
- El scraper usa *timeouts* y reintentos acotados; no ejecuta contenido remoto.
