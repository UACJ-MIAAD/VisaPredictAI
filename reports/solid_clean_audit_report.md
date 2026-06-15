# AUDITORÍA SOLID & CLEAN CODE — VisaPredictAI

_Revisión de diseño y limpieza de los scripts Python del pipeline. Severidad:_
_🔴 estructural · 🟡 importante · 🔵 mejora. Cada hallazgo con archivo y el porqué._

---

## Resumen ejecutivo

| Principio / práctica | Antes | Estado |
|---|:--:|---|
| **S** — Single Responsibility | 2 | 🟡 funciones que hacen 4–5 cosas |
| **O** — Open/Closed | 1 | 🔴 if-ladders y listas hardcoded |
| **L** — Liskov | — | n/a (sin herencia) |
| **I** — Interface Segregation | — | n/a (sin interfaces) |
| **D** — Dependency Inversion | 1 | 🔴 acoplamiento directo a requests/BS4/FS |
| **DRY** | 1 | 🔴→🟢 **corregido en este audit** |
| Nombres | 3 | 🟡 `df`/`final_action_dates` engañosos |
| Tamaño de función | 3 | 🟡 2 funciones largas |
| Manejo de errores | 2 | 🟡 `except: pass` silencioso |
| Estado global | 2 | 🟡 `mega_audit` muta globals |
| Configuración | 2 | 🟡 constantes dispersas |

**Veredicto: el código es legible y bien comentado, pero su *diseño* es de script, no de módulo.** El mayor pecado —duplicación masiva entre los dos scrapers— se corrige en este audit; el resto queda como plan priorizado.

---

## 🔴 Estructurales

### DRY-1 · Dos scrapers ~80 % duplicados — **CORREGIDO**
`scrape_visa_bulletins.py` y `scrape_family_visa_bulletins.py` compartían **6 funciones lógicamente idénticas** (`get_soup`, `extract_month_links`, `extract_datetime_from_link`, `string_to_datetime`, `classify_status`, `_norm_label`) más constantes (`BASE_URL`, lista de países, formato de fecha). Un bug en cualquiera había que arreglarlo dos veces (y de hecho el fix de retry y el de `NA→UNK` se aplicaron por duplicado).
**Fix aplicado:** extraídas a **`visa_common.py`** (única fuente de verdad); los scrapers ahora importan de ahí y conservan sólo lo que difiere (detección de sección, mapeo de categoría, columnas de salida). **607 → 365 líneas** en los scrapers + 129 compartidas; verificado byte-idéntico en la salida.

### OCP-1 · Cerrado a la extensión
- Añadir un país obliga a editar la lista en `visa_common` **y** el `CANONICAL` de `build_panel`/`mega_audit` (duplicado entre ambos).
- `classify_eb_category` / `classify_family_category` son **escaleras de `if`**: una nueva etiqueta EB-5 = editar la función. Una tabla de reglas declarativa (lista de `(predicado, código)`) abriría la extensión sin tocar el motor.
**Recomendación:** mapa de países en un `config.py` ligero (sin `import requests`); clasificadores guiados por datos.

### DIP-1 · Acoplamiento a implementaciones concretas
Los scrapers dependen directamente de `requests`, `BeautifulSoup`, la URL hardcoded y `to_csv` a rutas fijas (`data/{country}_...csv`). Consecuencia práctica: **las funciones de extracción sólo se pueden probar con red** — por eso los tests cubren los clasificadores puros pero **no** `extract_tables`/`extract_country_data`.
**Recomendación:** inyectar un `fetcher` (callable `url -> html`) y un `sink` (writer); permite tests con fixtures HTML guardados (sin red) y un modo offline reproducible.

---

## 🟡 Importantes

### SRP-1 · Funciones con múltiples responsabilidades
- `extract_country_data` (≈45 L): selecciona columnas + renombra + convierte fecha + calcula `visa_wait_time` + clasifica estado + mapea categoría + deduplica. Son **6 responsabilidades** en una función.
- `extract_tables` (≈50 L): detecta sección + cuenta tablas FAD/DFF + parsea filas + decide encabezado + etiqueta. 
- `main` (≈25 L): descubre links + crawlea + reporta fallos + extrae por país + escribe.
**Recomendación:** descomponer en pasos nombrados (p.ej. `select_columns`, `annotate`, `to_canonical`, `deduplicate`) — además testeables aislados.

### NAME-1 · Nombres engañosos
- La columna **`final_action_dates`** guarda fechas de **FAD *y* DFF** (el `table_type` las distingue) — el nombre miente para las filas DFF. Debería ser `priority_date` ya en el scraper (el panel sí lo renombra).
- `string_to_datetime` **no siempre** devuelve datetime (devuelve `None` para C-via-bulletin… en realidad devuelve la fecha del boletín para 'C'); nombre + comportamiento divergen.
- `df`, `dfs`, `df_subset`, `c`, `b`, `t`: monoletras fuera de comprehensions cortas.

### ERR-1 · `except: pass` silencioso
`extract_country_data` envuelve la selección de columnas en `try/except Exception: pass` — si un mes tiene una forma inesperada, se descarta **sin rastro** (el mismo patrón que ya causó la pérdida silenciosa de dic-2007 en el crawler). 
**Recomendación:** capturar excepción específica (`KeyError`) y **registrar** el `(país, mes)` saltado.

### GLOBAL-1 · Estado global mutable
`mega_audit.py` acumula el reporte en `L` y `FLAGS` a nivel módulo, mutados por cada `d*` función. Imposible de correr dos veces en el mismo proceso sin contaminación; difícil de testear.
**Recomendación:** una clase `Report` o pasar/retornar el acumulador.

### CFG-1 · Configuración dispersa
La paleta UACJ está **duplicada** en los dos visualizadores; `BASE` (epoch) vive en `build_panel`; `DEAD_MONTHS` en `mega_audit`; rutas `data/` repetidas en 5 archivos.
**Recomendación:** un `config.py` único (paleta, epoch, países canónicos, rutas, meses muertos).

---

## 🔵 Mejoras

- **TYPE-1** · Type hints inconsistentes: los scrapers están parcialmente tipados; `build_panel`/`audits` casi sin hints. Añadir y correr `mypy`.
- **DOC-1** · Los scrapers carecían de docstring de módulo (sí lo tienen `build_panel`, `mega_audit`, tests).
- **FMT-1** · Sin formateador/linter (`ruff`/`black`); estilo manual (mezcla de comillas, líneas largas). Añadir `ruff` a CI.
- **MAGIC-1** · Números mágicos: backoff `2*(attempt+1)`, `thresh_years=8`, `min_rows=20_000`, `threshold=0.12`. Nombrarlos como constantes.

---

## Lo que YA está bien (no regresar)

- **Comentarios que explican el *porqué*** (deriva de formato, orden de desambiguación, footgun de pandas) — ejemplares.
- **Funciones de clasificación puras** (sin I/O) — fáciles de testear; ya cubiertas por `tests/test_parsers.py`.
- **Idempotencia** de `build_panel`; clave única garantizada.
- **`if __name__ == "__main__"`** en todos los ejecutables.
- **Guardas de robustez** (retry, reporte de fallos, dedup defensivo).

---

## Roadmap priorizado

| Prioridad | Acción | Principio | Esfuerzo |
|:--:|---|---|:--:|
| ✅ hecho | Extraer `visa_common.py` (deduplicar scrapers) | DRY/SRP | — |
| 1 | `config.py` ligero (paleta, epoch, países, rutas, dead months) | CFG/OCP | Bajo |
| 2 | Descomponer `extract_country_data` en pasos nombrados | SRP | Medio |
| 3 | Inyectar `fetcher`/`sink` → tests de extracción con fixtures | DIP | Medio |
| 4 | Renombrar `final_action_dates`→`priority_date` en el scraper | Naming | Bajo |
| 5 | `except KeyError` + log en vez de `except: pass` | Err handling | Bajo |
| 6 | Clasificadores guiados por tabla de reglas | OCP | Medio |
| 7 | `ruff`/`black` + type hints + `mypy` en CI | Estilo | Bajo |

_Generado 14-jun-2026. Complementa `mlops_audit_report.md` (madurez de proceso) y `mega_audit_report.md` (calidad del dato)._
