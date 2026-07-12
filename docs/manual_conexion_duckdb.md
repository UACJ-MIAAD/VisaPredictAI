# Manual de conexión — Base de datos DuckDB

**VisaPredict AI · panel multiserie y(p,c,b,t)**

Este manual reúne **todas las formas de consultar** el almacén DuckDB del proyecto:
desde Python, la línea de comandos, la interfaz web oficial y dos aplicaciones de
escritorio. Elige la que más te acomode; todas leen el **mismo archivo**.

---

## 1. La base de datos

| | |
|---|---|
| **Archivo** | `data/processed/visapredict.duckdb` (≈ 11 MB) |
| **Ruta absoluta** | `/Users/haowei/Documents/Anteproyecto/VisaPredictAI/data/processed/visapredict.duckdb` |
| **Contenido** | 12 tablas + 6 vistas/marts (esquema estrella con migraciones versionadas) |
| **Fuente de verdad** | `data/processed/visa_panel_long.csv` (versionado en git) |
| **El `.duckdb`** | derivado y **regenerable** — está en `.gitignore` |
| **Regenerar** | `make db` (corre `pipeline/build_database.py`) |

> El `.duckdb` y el `.parquet` se reconstruyen de cero con `make db`. Si los borras o
> corrompes, no pasa nada. **Nunca edites la base a mano**: cambia el scraper o el CSV.

---

## 2. Reglas importantes antes de conectar

**Regla del candado (un solo escritor).** DuckDB permite **un solo proceso con el
archivo abierto en escritura** a la vez. Varios procesos pueden abrirlo en
**solo-lectura** simultáneamente, pero entonces ninguno puede escribir.

- Para **explorar**, abre siempre en **solo-lectura** (read-only). Así nunca chocas con
  `make db` ni con otra app.
- **No** tengas dos herramientas en modo escritura sobre el archivo al mismo tiempo.
- Antes de correr `make db`, cierra las apps/UI que tengan la base abierta (o ábrelas
  read-only).

---

## 3. Formas de conexión

| Método | Tipo | Requiere instalar | Mejor para |
|---|---|---|---|
| A. Python (venv `ante`) | Script / REPL | Nada (ya está) | Automatizar, scripts |
| B. DuckDB CLI | Terminal | `brew install duckdb` | Consultas rápidas |
| C. DuckDB Web UI | Navegador (oficial) | Nada extra (usa el CLI/módulo) | Explorar + gráficas |
| D. TablePlus | App nativa Mac | `brew install --cask tableplus` | Navegar, exportar |
| E. DBeaver | App nativa (Java) | `brew install --cask dbeaver-community` | Multi-BD |
| F. VS Code | Editor | Extensión "DuckDB" | Quedarte en el editor |

---

### A. Python (ya instalado en el venv `ante`)

El módulo `duckdb` ya está en el entorno. Una consulta rápida:

```bash
cd /Users/haowei/Documents/Anteproyecto/VisaPredictAI
source ante/bin/activate
python -c "import duckdb; print(duckdb.connect('data/processed/visapredict.duckdb', read_only=True).execute('SELECT * FROM mart_series_summary LIMIT 10').fetchdf())"
```

En un script:

```python
import duckdb
con = duckdb.connect("data/processed/visapredict.duckdb", read_only=True)
df = con.execute("SELECT * FROM mart_training_F WHERE country='mexico'").fetchdf()
print(df)
con.close()
```

> Usa siempre `read_only=True` para consultar; evita bloquear el archivo.

---

### B. DuckDB CLI (terminal)

Instalar una sola vez:

```bash
brew install duckdb
```

Abrir un shell SQL interactivo:

```bash
cd /Users/haowei/Documents/Anteproyecto/VisaPredictAI
duckdb -readonly data/processed/visapredict.duckdb
```

Comandos útiles dentro del shell:

```text
.tables                  -- lista tablas y vistas
.schema fact_priority    -- ver el DDL de una tabla
.mode box                -- salida en cajas bonitas
SELECT * FROM mart_series_summary LIMIT 10;
.quit
```

Una sola consulta sin entrar al shell:

```bash
duckdb data/processed/visapredict.duckdb -c "SELECT country, count(*) FROM mart_series_summary GROUP BY 1"
```

---

### C. DuckDB Web UI (navegador) — la interfaz oficial

Es un servidor local que se ve en el navegador (`http://localhost:4213`). No sube nada a
la nube. La forma directa, teniendo el CLI:

```bash
cd /Users/haowei/Documents/Anteproyecto/VisaPredictAI
duckdb -ui data/processed/visapredict.duckdb
```

**Para que conviva con otra app abierta** (p. ej. TablePlus en read-only), lánzala
adjuntando la base en solo-lectura — así ningún proceso bloquea al otro:

```bash
cd /Users/haowei/Documents/Anteproyecto/VisaPredictAI
source ante/bin/activate
python -c "import duckdb,time; c=duckdb.connect(); c.execute(\"ATTACH 'data/processed/visapredict.duckdb' AS visapredict (READ_ONLY)\"); c.execute('CALL start_ui()'); time.sleep(43200)"
```

Dentro de la UI, la base aparece como **`visapredict`** en el panel izquierdo. En una
celda SQL, fíjala como activa y luego consulta normal:

```sql
USE visapredict;
SELECT * FROM mart_series_summary ORDER BY n_trainable DESC LIMIT 25;
```

> En la web UI: clic en una columna te da estadísticas y distribución; los resultados se
> pueden ver como **gráfica** sin escribir código.

---

### D. TablePlus (app nativa de Mac)

Instalar una sola vez:

```bash
brew install --cask tableplus
```

1. Abre TablePlus → **Create Connection** → elige **DuckDB**.
2. Llena:
   - **Name:** `VisaPredict`
   - **Database path:** la ruta absoluta del archivo (ver sección 1), o usa
     **"Select file..."**.
   - **Read Only:** déjalo **activado** (recomendado).
3. **Test** → **Connect**.

TablePlus trae DuckDB nativo (no descarga drivers). La versión gratuita basta para
explorar (limita a ~2 pestañas a la vez).

---

### E. DBeaver Community (app nativa, multi-BD)

Instalar una sola vez:

```bash
brew install --cask dbeaver-community
```

1. **Nueva conexión** (icono enchufe "+") → busca **DuckDB** → Next.
2. **Path:** la ruta absoluta del archivo (sección 1).
3. La primera vez, DBeaver pide **descargar el driver de DuckDB** → *Download*.
4. Marca la conexión como **Read-only** (pestaña *Connection details*).
5. *Finish* y explora.

> Si DBeaver da error de **versión** ("incompatible storage / newer version"), el driver
> es más viejo que el archivo (creado con DuckDB 1.5.3). Ve a
> *Database → Driver Manager → DuckDB → Edit → Libraries* y baja el driver **≥ 1.5.3**.

---

### F. VS Code (extensión)

Si ya usas VS Code: abre **Extensiones** (Cmd+Shift+X), busca **"DuckDB"** e instala una.
Te deja correr SQL contra el archivo `.duckdb` sin salir del editor.

---

## 4. Consultas de arranque

El repo incluye **`docs/example_queries.sql`** con 14 consultas validadas (el panel, el
set entrenable, Diversity Visa, linaje de etiquetas, jerarquía, retrogresiones…).
Ábrelo en cualquiera de las herramientas y corre las que quieras.

Desde el CLI puedes correr todo el archivo de un jalón:

```bash
duckdb -readonly data/processed/visapredict.duckdb < docs/example_queries.sql
```

---

## 5. Catálogo rápido

**Vistas y marts (lo más útil para consultar):**

| Vista | Qué da |
|---|---|
| `v_panel_long` | El panel tidy completo y(p,c,b,t) (reconstrucción sin pérdida del CSV) |
| `mart_training_F` | Solo lo entrenable (estado `F`) con la variable dependiente |
| `mart_series_summary` | Resumen por serie (para filtrar las "evaluables") |
| `v_dv_long` | Diversity Visa (rango regional; dataset aparte, no objetivo predictivo) |
| `v_category_alias` | Linaje: qué etiquetas crudas se volvieron cada categoría canónica |
| `v_trainable_by_preference` | Roll-up por nivel de preferencia (EB-5 plegado) |

**Tablas base:** `dim_area`, `dim_category`, `dim_table`, `dim_date`, `dim_status`,
`dim_region`, `dim_category_alias`, `fact_priority`, `fact_dv_rank`, `source_artifact`,
`schema_version`, `etl_run`.

---

## 5 bis. Auditoría de procedencia (fila → fuente → run)

Cada fila de hechos enlaza la corrida que la cargó (`etl_run_id` → `etl_run`, con
identidad completa del build: `git_sha`, hashes del panel/locks, `build_status`) y
su mes resuelve al HTML congelado que lo publicó (`source_artifact`: archivo,
`sha256`, URI de archivo en S3, licencia). La consulta canónica — la ejecuta tal
cual `tests/test_provenance_chain.py`, extraída de este documento, para que el
manual jamás se desfase de lo que corre:

<!-- provenance-audit-query:begin -->
```sql
SELECT
    a.slug            AS country,
    c.code            AS category,
    t.code            AS "table",
    d.bulletin_date,
    f.status,
    f.raw_value,
    s.filename        AS source_file,
    s.sha256          AS source_sha256,
    s.url             AS source_url,
    r.run_id,
    r.git_sha,
    r.pipeline_run_id,
    r.build_status
FROM fact_priority f
JOIN dim_area     a ON a.area_id     = f.area_id
JOIN dim_category c ON c.category_id = f.category_id
JOIN dim_table    t ON t.table_id    = f.table_id
JOIN dim_date     d ON d.date_id     = f.date_id
JOIN etl_run      r ON r.run_id      = f.etl_run_id
LEFT JOIN source_artifact s ON s.vintage = d.bulletin_date
ORDER BY d.bulletin_date, a.slug, c.code, t.code;
```
<!-- provenance-audit-query:end -->

> `source_artifact` se llena desde `data/snapshots/` (gitignored; máster en S3).
> En un clon sin snapshots la tabla queda vacía y el build lo registra en
> `etl_run.build_status='degraded'` — el `LEFT JOIN` lo hace visible (columnas
> `source_*` en NULL), nunca silencioso.

---

## 6. Solución de problemas

| Síntoma | Causa | Solución |
|---|---|---|
| `Could not set lock` / `database is locked` | Otra app/proceso tiene el archivo en escritura | Cierra esa app, o abre todo en **read-only** |
| `Catalog "_duckdb_ui" does not exist` | Abriste la Web UI en read-only (no puede crear su estado) | Ábrela en read-write, o adjunta la base read-only (sección C) |
| DBeaver: error de versión / storage | Driver de DuckDB más viejo que 1.5.3 | Actualiza el driver a ≥ 1.5.3 (Driver Manager) |
| Puerto 4213 ocupado | Quedó una Web UI corriendo | `lsof -ti tcp:4213 \| xargs kill` |
| La base "desapareció" | Borraste el `.duckdb` (es regenerable) | `make db` la reconstruye |

---

*Generado para el repositorio VisaPredict AI. La base es un artefacto de datos; las
cifras no constituyen asesoría legal.*
