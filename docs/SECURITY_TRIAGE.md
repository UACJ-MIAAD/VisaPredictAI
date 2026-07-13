# Triage de dependencias Python y política de SLA (G5 · A5)

Gate programado: `.github/workflows/scheduled-quality.yml` (lunes + dispatch) corre
`pip-audit --disable-pip` sobre los **locks versionados de ambas plataformas**.
runtime/dev VETAN sin excepciones (dependencias directas: se arreglan por PR con tests
y rollback). El perfil model dejó de ser report-only (A5, plan 3 repos 2026-07-12):
ahora es **gate con allowlist explícita** — los avisos aceptados de la tabla de abajo
van como `--ignore-vuln` en el workflow; **cualquier aviso nuevo (incluido high/critical)
bloquea el job**. SBOM CycloneDX del perfil dev como artefacto por corrida (sin `|| true`:
un SBOM roto falla el job).

Misma política de SLA que el repo web (`VisaPredictAI_web/docs/SECURITY_TRIAGE.md`):
critical 48 h · high 7 días · moderate 30 días o el bump del upstream directo · low con
el siguiente upgrade. **Prohibido el auto-fix**: todo bump va por PR con suite verde.

## Matriz de locks por perfil × plataforma (A5, 2026-07-12)

| Perfil | macOS arm64 (referencia) | Linux x86_64 (CI/cron) | Hashes sha256 |
|---|---|---|---|
| runtime | `locks/runtime.txt` | `locks/runtime-linux-x86_64.txt` | Linux sí (`-r` + hash-checking) |
| dev | `locks/dev.txt` | `locks/dev-linux-x86_64.txt` | Linux sí (`-r` + hash-checking) |
| model-cpu | `locks/model-cpu.txt` (freeze de `ante/`) | `locks/model-cpu-linux-x86_64.txt` | **No** (ver abajo) |
| GPU/deep | `aws_gpu/ante_nf-requirements.lock` + `aws_gpu/requirements.txt` (pins) | — | No (entorno efímero EC2) |

- Los locks Linux se compilan con `uv pip compile --python-platform x86_64-unknown-linux-gnu
  --python-version 3.14 -c <lock macOS>` (el comando exacto va en el header de cada lock):
  el constraint contra el lock macOS garantiza **cero drift de versiones** entre la
  plataforma de referencia y la de CI (verificado en la generación: 22/22, 35/35 y
  87/87 pins compartidos idénticos).
- **Por qué model-cpu va sin hashes:** CI y el cron lo consumen como **constraints
  (`-c`)** — torch debe instalarse del índice CPU de PyTorch (el pin CUDA de PyPI pesa
  ~2 GB y arrastra `nvidia-*`), y pip no admite hashes en constraints files. Los
  `nvidia-*`/`triton` del lock Linux son inertes bajo `-c` (solo fijarían versión si
  algo los instalara).
- El backend de build está pinneado en `pyproject.toml` (`setuptools==81.0.0`) y el
  toolchain de instalación (`pip==26.1.2`, `setuptools==81.0.0`, `wheel==0.47.0`) en
  cada workflow que crea el entorno.
- ⚠️ `tools/make_locks.sh` (`make lock`) solo regenera los 3 locks macOS; los Linux se
  regeneran con los comandos `uv pip compile` de sus headers **en la misma tanda** que
  cualquier `make lock` (si divergieran, el gate semanal y el hash-checking de CI lo
  delatan).

## Resuelto al estrenar la política (2026-07-11)

- **requests 2.32.5 → 2.33.0** (CVE-2026-25645, runtime+dev): dependencia DIRECTA con
  fix disponible → actualizada en el acto (pyproject + locks regenerados de venvs
  frescos + suite de parsers/extracción verde — el único consumidor es la capa de
  scraping). `pip-audit` de runtime/dev: **limpio**.

## Triage vigente — perfil model (9 avisos en 7 paquetes, allowlist del gate)

Auditado el 2026-07-12 con `pip-audit --disable-pip` sobre `locks/model-cpu.txt`
(232 paquetes, entorno de referencia). El lock Linux del perfil (closure de
`.[dev,model]`, 107 paquetes) contiene un SUBCONJUNTO: solo torch y pytorch-lightning
de esta tabla (transformers/diskcache/msgpack/pydantic-settings/pypdf llegan a `ante/`
por herramientas locales fuera de los extras). Contexto común de superficie: **nada del
perfil model sirve tráfico** — torch/transformers/lightning corren OFFLINE en el
modelado local y en el bloque de modelado del cron (entrada: el panel propio, no datos
de terceros); no hay endpoint que los exponga.

| Paquete | Aviso | ¿Nos afecta? | Decisión | Owner | Revisión |
|---|---|---|---|---|---|
| torch 2.12.0 | CVE-2025-3000 (sin fix publicado) | BAJA: solo entrenamiento/inferencia offline sobre el panel propio; sin deserialización de modelos de terceros (checkpoints propios) | **Accept** (allowlist); vigilar release con fix | Javier | 2026-08-12 |
| pytorch-lightning 2.5.6 | PYSEC-2026-3043 (alias CVE-2026-31221 / GHSA-75m9-98v2-hjpm; sin fix) | BAJA: `load_from_checkpoint` llama `torch.load` sin `weights_only=True`; F2 carga SOLO checkpoints PROPIOS (entrenamiento local/campañas), nunca de terceros; sin superficie remota | **Accept** (allowlist); vigilar release del upstream | Javier | 2026-08-12 |

**Reconciliación ronda 10 (13-jul-2026):** el upgrade DELIBERADO `chronos-forecasting 2.3.1`
+ `transformers 5.13.1` + `pillow 12.3.0`, con `model-cpu` regenerado desde un venv FRESCO
`.[dev,model]` (antes `pip freeze` del `ante/` mutable), **cerró `PYSEC-2026-2290`** (RCE crítico
de transformers, CVSS 9.6) y **retiró 7 excepciones**: los 3 avisos de `transformers 4.57.6`
(PYSEC-2025-217, CVE-2026-1839, CVE-2026-4372 — 5.13.1 los corrige) y los de `diskcache`,
`msgpack`, `pydantic-settings` y `pypdf`, que **desaparecieron del cierre fresco** (no eran
dependencias reales del perfil). `Ray` también salió (huérfano, sin import). **No se añadió
ningún `--ignore-vuln` nuevo.** Verificado: `pip-audit -r` sobre `model-cpu.txt` y
`model-cpu-linux-x86_64.txt` reporta EXACTAMENTE estos 2 avisos; runtime/dev sin avisos.

**Regla de cierre:** al aplicar un upgrade (o cuando salga fix de un "Accept"), el PR
retira el `--ignore-vuln` correspondiente del workflow **y** la fila de esta tabla en
el mismo commit — la allowlist y el triage no pueden divergir.

**Lado npm (repo web):** 8 avisos moderate / 0 high-critical, triados con la misma
política y fecha de revisión en `VisaPredictAI_web/docs/SECURITY_TRIAGE.md`.

**Acciones/contenedores/locks (aceptación G5):** todas las Actions de ambos repos van
pinneadas por SHA; la imagen del gate LaTeX es la única "contenedor" (texlive/texlive,
job de verificación sin secretos); los locks por perfil y plataforma son la fuente del
audit y del SBOM. El repo web cubre su mitad en G2 (npm audit semanal + triage propio).
