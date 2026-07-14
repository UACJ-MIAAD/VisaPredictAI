# Triage de dependencias Python y política de SLA (G5 · A5)

Gate programado: `.github/workflows/scheduled-quality.yml` (lunes + dispatch) **y** el job
`supply-chain` de `ci.yml` (cada PR/push) corren `tools/audit_python_supply_chain.py`, que
audita los **9 locks versionados** con `pip-audit --disable-pip` (sin ocultar nada primero) y
reconcilia el resultado BRUTO contra la fuente única `security/python_advisories.json` con
**biyección exacta por perfil/lock**. runtime/dev VETAN sin excepciones (dependencias directas:
se arreglan por PR con tests y rollback). model y deep son **gate con allowlist explícita** — los
avisos aceptados viven en el JSON (ya NO como `--ignore-vuln` textual del workflow, que dejó de
ser autoridad en P0R.3); **cualquier aviso nuevo, o una excepción huérfana/expirada, bloquea el
job**. SBOM CycloneDX del perfil dev como artefacto por corrida (sin `|| true`: un SBOM roto falla
el job).

Misma política de SLA que el repo web (`VisaPredictAI_web/docs/SECURITY_TRIAGE.md`):
critical 48 h · high 7 días · moderate 30 días o el bump del upstream directo · low con
el siguiente upgrade. **Prohibido el auto-fix**: todo bump va por PR con suite verde.

## Matriz de locks por perfil × plataforma (A5, 2026-07-12)

| Perfil | macOS arm64 (referencia) | Linux x86_64 (CI/cron) | Hashes sha256 |
|---|---|---|---|
| runtime | `locks/runtime.txt` | `locks/runtime-linux-x86_64.txt` | Linux sí (`-r` + hash-checking) |
| dev | `locks/dev.txt` | `locks/dev-linux-x86_64.txt` | Linux sí (`-r` + hash-checking) |
| model-cpu | `locks/model-cpu.txt` (venv fresco `.[dev,model]`) | `locks/model-cpu-linux-x86_64.txt` | **No** (ver abajo) |
| deep (aislado, pandas 2.x) | `locks/deep-macos-arm64.txt` | `locks/deep-linux-x86_64-cpu.txt` · `locks/deep-linux-x86_64-cu126.txt` | **Sí** los 3 (`--generate-hashes`) |

- Los espejos Linux se compilan con `uv pip compile --python-platform x86_64-unknown-linux-gnu
  --python-version 3.14 --no-annotate -c <lock macOS staged>`: el constraint contra el lock
  macOS garantiza **cero drift de versiones** entre la plataforma de referencia y la de CI;
  `--no-annotate` evita que la ruta temporal de staging se filtre a las anotaciones.
- **Por qué model-cpu va sin hashes:** CI y el cron lo consumen como **constraints
  (`-c`)** — torch debe instalarse del índice CPU de PyTorch (el pin CUDA de PyPI pesa
  ~2 GB y arrastra `nvidia-*`), y pip no admite hashes en constraints files. Los
  `nvidia-*`/`triton` del lock Linux son inertes bajo `-c` (solo fijarían versión si
  algo los instalara).
- El backend de build está pinneado en `pyproject.toml` (`setuptools==81.0.0`) y el
  toolchain de instalación (`pip==26.1.2`, `setuptools==81.0.0`, `wheel==0.47.0`) en
  cada workflow que crea el entorno.
- **`tools/make_locks.sh` (`make lock`) regenera los 9 locks en UNA tanda** (3 base macOS +
  3 espejos Linux + 3 deep) y los promueve con `tools/promote_lockset.py` mediante **rollback
  transaccional + detección de matriz parcial** (NO atomicidad de bundle): valida el staging con
  el contrato estático único (`tools/lock_contracts.py`), hace rename por lock, escribe el
  manifiesto `locks/lockset.json` AL FINAL (ligando hashes de locks + fuentes, incluidos los 3
  scripts del contrato) y se autovalida ⇒ una interrupción deja árbol y manifiesto divergentes y
  el auditor recalcula ambos y BLOQUEA. Toolchain PINEADO y sin fecha ⇒ REGENERAR es repetible
  bajo el mismo estado del índice (la instalación desde los locks sí es byte-reproducible). El
  **perfil deep** (aislado, pandas 2.x) tiene su fuente directa en `requirements/deep.in`
  (+ wrappers `deep-linux-{cpu,cu126}.in`), separado de `pyproject.toml` porque el stack deep exige
  pandas 2.x mientras el base fija pandas 3. **CI instala de verdad** los locks deep CPU/macOS
  (job `deep-lock-install`: `--require-hashes` + `pip check` + smoke con tensor finito); CUDA queda
  en resolución+hash+audit (su ejecución A10G se certifica en P0R.5).

## Resuelto al estrenar la política (2026-07-11)

- **requests 2.32.5 → 2.33.0** (CVE-2026-25645, runtime+dev): dependencia DIRECTA con
  fix disponible → actualizada en el acto (pyproject + locks regenerados de venvs
  frescos + suite de parsers/extracción verde — el único consumidor es la capa de
  scraping). `pip-audit` de runtime/dev: **limpio**.

## Triage vigente — perfiles model y deep (1 avisos en 1 paquete, allowlist del gate)

Auditado el 2026-07-13 (ronda 10, P0R.4) con `pip-audit --disable-pip` sobre los **9 locks**
(runtime/dev/model-cpu × macOS+Linux + los 3 deep hasheados), reconciliado por
`tools/audit_python_supply_chain.py` contra `security/python_advisories.json` con biyección
exacta por perfil/lock. El único aviso restante (pytorch-lightning) aparece en los 2 locks
model **y** los 3 deep. El upgrade DELIBERADO `torch 2.12.0 → 2.12.1` (P0R.4) **cerró
`CVE-2025-3000`** — ya no se observa en ningún lock (torch limpio en model y deep, `+cpu`/`+cu126`
incluidos, verificado además con la consulta de versión pública normalizada). Contexto común de
superficie: **nada de model/deep sirve tráfico** — torch/transformers/lightning corren OFFLINE
en el modelado local, el bloque de modelado del cron y el bundle EC2 efímero (entrada: el panel
propio, no datos de terceros); no hay endpoint que los exponga.

| Paquete | Aviso | ¿Nos afecta? | Decisión | Owner | Revisión |
|---|---|---|---|---|---|
| pytorch-lightning 2.5.6 (perfiles model y deep) | PYSEC-2026-3043 (alias CVE-2026-31221 / GHSA-75m9-98v2-hjpm; sin fix) | BAJA: `load_from_checkpoint` llama `torch.load` sin `weights_only=True`; F2 carga SOLO checkpoints PROPIOS (entrenamiento local/campañas), nunca de terceros; sin superficie remota | **Accept** (allowlist); vigilar release del upstream | Javier | 2026-08-12 |

**Reconciliación P0R.4 (13-jul-2026):** el upgrade DELIBERADO `torch 2.12.0 → 2.12.1` **cerró
`CVE-2025-3000`** y retiró su excepción (torch queda limpio en los 5 locks que lo pinan). Se
añadió el **perfil deep aislado** (`requirements/deep.in`, pandas 2.x) con 3 locks HASHEADOS
que sustituyen al viejo `aws_gpu/ante_nf-requirements.lock` (freeze mutable de 12 pins con
`torch==2.12.0` vulnerable, ahora borrado); `pytorch-lightning` extendió su allowlist a
`["model","deep"]` porque neuralforecast 3.1.9 exige `<2.6.0`. La ronda 10 previa ya había
cerrado `PYSEC-2026-2290` (RCE crítico de transformers, CVSS 9.6) vía `transformers 5.13.1`.
**No se añadió ninguna excepción nueva.** Verificado: el runner reporta EXACTAMENTE este 1
aviso (pytorch-lightning en model+deep); runtime/dev sin avisos, torch sin avisos.

**Regla de cierre:** al aplicar un upgrade (o cuando salga fix de un "Accept"), el PR
retira la entrada del `security/python_advisories.json` **y** la fila de esta tabla en
el mismo commit — la allowlist machine-readable y el triage no pueden divergir (lo verifica
`tools/check_supply_chain_triage.py` en el job `consistency`). Así se cerró CVE-2025-3000
en P0R.4 (torch 2.12.0 → 2.12.1).

**Lado npm (repo web):** 8 avisos moderate / 0 high-critical, triados con la misma
política y fecha de revisión en `VisaPredictAI_web/docs/SECURITY_TRIAGE.md`.

**Acciones/contenedores/locks (aceptación G5):** todas las Actions de ambos repos van
pinneadas por SHA; la imagen del gate LaTeX es la única "contenedor" (texlive/texlive,
job de verificación sin secretos); los locks por perfil y plataforma son la fuente del
audit y del SBOM. El repo web cubre su mitad en G2 (npm audit semanal + triage propio).
