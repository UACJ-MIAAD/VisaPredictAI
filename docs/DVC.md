# DVC en este repositorio

DVC está **inicializado** (`dvc init`) pero deliberadamente **no versiona los CSV
abiertos**. Esta nota explica por qué y para qué se reserva, adaptando la práctica
del repo hermano EpiForecast-MX al caso de visas.

## Decisión: los CSV abiertos se quedan en git

Los CSV por país (`data/raw/`) y el panel (`data/processed/`) son el **entregable de
datos abiertos** del proyecto: cualquiera los descarga directamente del repositorio,
sin necesitar DVC ni credenciales de un remoto S3. Versionarlos con DVC los sacaría
de git y **rompería esa accesibilidad**. Lo que los protege es que `dvc.yaml` los declara
como **salidas `cache: false` del DAG**: DVC los rastrea por hash pero viven en git, y un
`dvc add data/` accidental es **rechazado** (la ruta ya pertenece a un stage). El
`.dvcignore` ya NO usa el patrón `data/**/*.csv` (impediría rastrearlos como salidas;
ver la nota dentro del propio `.dvcignore`). El *bloat* histórico de git venía de
las **figuras binarias**, ya resueltas (gitignored; regenerar con `make figures`).

De los binarios derivados del panel, **solo `visa_panel_long.parquet`** se versiona
en la cache DVC → S3 (es byte-determinista; salida con cache del stage `database`).
**`visapredict.duckdb` NO se versiona en absoluto**: embebe orden interno/timestamps y no
es byte-determinista, así que declararlo como `out` dejaba `dvc repro` perpetuamente
sucio (decisión documentada en `dvc.yaml`). Es un efecto secundario del stage,
gitignored y reconstruible con `make db`. El CSV abierto sigue siendo la fuente de
verdad versionada en git.

## Qué versiona DVC HOY (activo)

La fase de modelado ya llegó, así que DVC **ya está en uso**. Pointers `*.dvc` commiteados
(ver `git ls-files '*.dvc'`):

- **`models.dvc`** — checkpoints/finalistas de modelos (no reproducibles barato).
- **`mlflow.db.dvc`** — historia de experimentos MLflow (no reproducible en git).
- **`visapredict.duckdb` + `visa_panel_long.parquet`** — binarios derivados; ya **no** son
  pointers `*.dvc` sueltos: son **salidas con cache del stage `database` del DAG** (ver abajo),
  igual versionados en la cache DVC → S3, reconstruibles con `make repro`/`make db`.

El remoto es S3 (`make sync` → `dvc push` + commit de los pointers/lock). Un clon que quiera los
binarios sin re-construir hace `dvc pull` **con credenciales S3 del proyecto**; sin ellas,
`make repro` los regenera (parquet/duckdb) — los `models/`/`mlflow.db` solo vía `dvc pull`.

## Pipeline reproducible como DAG (`dvc.yaml` + `dvc.lock`)

`dvc.yaml` declara el pipeline de datos como un **grafo de dependencias** que `dvc repro`
(`make repro`) reconstruye **en orden, determinísticamente y solo lo que cambió**:

```
                 scrape  (parsea data/snapshots/ OFFLINE → data/raw/*.csv)
                   │
                 panel   (→ data/processed/visa_panel_long.csv)
        ┌──────────┼──────────┐
   bulletins    key_facts   database
   (feed web)  (key_facts.  (DuckDB estrella
                json+.tex)   + parquet → cache DVC)
```

- **Raíz = `data/snapshots/`** (HTML congelado). La única fetch en vivo es `pipeline/freeze_snapshots.py`
  (red), que queda **fuera** del DAG a propósito: el DAG es 100 % offline y determinista.
- **`cache: false`** en los artefactos abiertos (`data/raw`, panel CSV, `bulletins.json`,
  `key_facts.json/.tex`): el DAG los **rastrea por hash** (en `dvc.lock`) pero los deja
  **versionados en git** — siguen siendo el entregable descargable sin DVC.
- **`cache: true`** solo en los binarios (`visapredict.duckdb`, `parquet`): van a la cache DVC → S3.
- **Determinismo:** `make repro` dos veces seguidas = *"Data and pipelines are up to date"*; un
  rebuild produce datos byte-idénticos. `bulletins.json` sella su recencia con el **último mes
  de boletín** (no la hora de pared; override con `SOURCE_DATE_EPOCH`), así no hay *churn*.
- **`dvc.lock` committeado = la prueba de reproducibilidad**: fija el hash de cada entrada y
  salida del grafo. `make dag` imprime el grafo; `make repro-force` re-ejecuta todo.

> Las **figuras del `.tex`** NO están en el DAG: necesitan el extra de modelado (`.[model]`);
> se regeneran con `experiments/make_*_figures.py` / `make figures`. El DAG cubre la cadena de **datos puros**.

## Cómo activarlo cuando haga falta

```bash
# 1) provisionar un remoto (S3, GCS, GDrive…) y configurarlo
dvc remote add -d storage s3://<bucket>/visapredict
dvc remote modify storage region us-east-1

# 2) versionar un artefacto de modelo (NO los CSV abiertos)
dvc add checkpoints/best_model.ckpt
git add checkpoints/best_model.ckpt.dvc checkpoints/.gitignore
git commit -m "data: track model checkpoint with DVC"

# 3) subir/bajar
dvc push        # sube al remoto
dvc pull        # baja en otro clon

# en la GitHub Action, las credenciales del remoto van como secrets.
```

`dvc` es una dependencia de desarrollo (no se necesita en runtime ni en el CI de
código hasta que existan artefactos versionados).

## Frontera DAG-determinista vs runner-transaccional (C1/C2, plan auditoría 2026-07-11)

El DAG contiene SOLO derivaciones **puras y byte-deterministas** de insumos versionados
(verificado por doble corrida y por el gate de CI que re-reproduce los stages de facts
con `dvc repro --force --single-item` y exige `git diff` limpio). Siete stages: scrape →
panel → {bulletins, database, key_facts, eda_facts, fe_facts}.

**Fuera del DAG, a propósito** (el runner transaccional es el cron `freeze_and_rebuild.yml`):

| Qué | Por qué |
|---|---|
| Ledgers (`forecast_log*.csv`) | Estado append-only con identidad de freeze (A2): *reproducir jamás reescribe evidencia operacional* |
| Forecasts del demostrador + scoring | Congelan estado (añadas) además de derivar; el cron los corre y commitea |
| Manifiesto de release | Hashea el estado (ledgers incluidos) y lleva `generated_at` — es una foto del corte, no una derivación |
| Figuras, galerías y PDFs | Timestamps embebidos (no byte-deterministas) + extra `.[model]` |
| `.duckdb` | Binario no byte-determinista (hallazgo del audit dúo); efecto secundario reconstruible |

**Portabilidad (C2):** los `cmd` usan `python` a secas — resuelto del entorno activo.
`make repro` antepone el bin del venv al PATH; en CI los comandos corren con el Python
del runner (la reproducción parcial de CI es, de paso, la prueba de clone limpio).
Los stages `eda_facts`/`fe_facts` requieren `pip install -e .[model]`.

**Semillas y tolerancias:** todo lo estocástico pasa por `config.seed_everything()`;
`config.run_metadata()` registra semilla, versiones de librerías y linaje de datos por
corrida. La única tolerancia conocida es la deriva numérica menor de la optimización de
SARIMA entre máquinas (documentada en `docs/FORECAST_EVAL.md`, limitación 6) — por eso
forecasts no son stage DVC y los facts sí (byte-exactos).
