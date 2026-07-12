# Política de artefactos y almacenamiento (I3, plan auditoría 2026-07-11)

Inventario de QUÉ vive DÓNDE, con tamaño al corte 2026-07-11, retención, consumidor y
ruta de restauración. La métrica se re-mide en cada corrida del cron (línea "Tamaños"
del correo SES) para ver crecimiento mes a mes. **La línea del SES es `du` del checkout
del runner** (clone fresco ≈ versionado + lo generado en el run); en un árbol de
desarrollo local `du` incluye además regenerables gitignored — para el tamaño EN GIT usar
`git ls-files -z <dir> | xargs -0 stat -f%z | awk '{s+=$1} END {print s/1024/1024}'`.

| Clase | Dónde vive | Tamaño (11-jul) | Retención | Consumidor | Restauración |
|---|---|---|---|---|---|
| Fuentes + tests + docs | git (`main`) | (dentro de .git 203 MB) | permanente | todo | clone |
| CSVs abiertos (raw/panel/bulletins) | git, `cache:false` (entregable descargable) | data/ **4.1 MB versionados** (árbol local 47 MB: + .duckdb/.parquet regenerables, gitignored) | permanente | web, DAG, tesis | `dvc repro` / clone |
| HTML congelado (296 boletines) | **S3 `visapredictai-raw-snapshots`** (versioning ON) = fuente de verdad; local gitignored | 32 MB | permanente | scrape offline | `aws s3 sync` (make update) |
| `.duckdb` / Parquet | regenerable; Parquet en cache DVC→S3 | (no en git) | regenerable | modelado | `make db` / `dvc pull` |
| reports/ (ledgers, scorecards, facts, galerías×4, PDFs, campañas) | git | **48.4 MB versionados** (⚠️ corrección auditoría 11-jul: el árbol local pesa 640 MB, pero ~590 MB son regenerables IGNORADOS — logs/estudios Optuna, PDFs y galerías locales; el rubro dominante EN GIT sigue siendo este, no 640) | ledgers/facts permanentes; galerías/PDFs = última añada (se REEMPLAZAN, no se acumulan versiones); campañas = procedencia permanente | web (fetch), RAG, .tex, gates | regenerables del DAG/cron salvo ledgers (append-only, actas git) |
| Manifiesto + contratos | git (`reports/release/`, `vp_data/contracts/`) | <1 MB | permanente | loader web (B2/B3) | `make release-manifest` |
| Modelo de embeddings (~118 MB q8) + ORT wasm | `public/` del web (gitignored el modelo; ORT vendorizado) | web public/ 190 MB | por versión de modelo | VisaBot (consent-gated) | HF hub / re-vendorizar |
| Locks por perfil + SBOM | git (`locks/`) + artefacto CI semanal (30 días) | ~0 | por upgrade auditado | instalaciones/audit | `make lock` |
| PDFs LaTeX compilados | artefactos del gate CI (14 días) — NUNCA en git | — | 14 días | revisión del autor | re-run del gate / Overleaf |

## Reglas

1. **Git conserva fuentes y artefactos AUDITABLES** (ledgers con actas, facts, manifiesto);
   lo regenerable pesado (duckdb, venvs, snapshots locales) va gitignored con máster
   externo (S3) o receta de regeneración.
2. **El web no duplica historia**: consume el corte VIGENTE por manifiesto+hashes (B2);
   sus `public/data/` son fallbacks del último corte, reemplazados, no acumulados.
3. **Overleaf compila desde git** — `reports/latex/` completo (fuentes+figuras) permanece
   en git sin excepción (crítico: Figures/ no se mueve, don't #7 del proyecto).
4. **Jamás reescribir historia sin decisión y backup** — el único rewrite sancionado fue
   el 20-jun-2026 (limpieza de autoría, con backup en `~/visapredict_backup_20jun/`).
5. **Crecimiento vigilado, poda deliberada**: si reports/ o .git crecen material y
   sostenidamente (línea del SES), la poda (p. ej. mover campañas viejas a DVC→S3) es
   una decisión de PR con este documento actualizado — nunca un script automático.
