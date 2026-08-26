# Threat Model — VisaPredict AI · Pipeline de datos (`UACJ-MIAAD/VisaPredictAI`)

> **Owner:** Javier Rebull (`jrebull`). **Fecha:** 2026-08-26 (v2, sustituye a la v1 del 2026-07-12).
> Documento hermano del producto web: `VisaPredictAI_web/docs/THREAT_MODEL.md`.
> Decisión que motiva esta versión: `docs/adr/0002-supply-chain-gates-retired.md`.

## 1. Alcance y postura

Cubre la cadena **scrape → S3 → panel → DuckDB → modelado → release → web**, el correo SES y la cadena
de suministro de CI/cron. Los datos del dominio son **públicos** (Visa Bulletin oficial); el repositorio es
académico, de **un solo autor**, sin servicio expuesto a internet. La propiedad crítica es **INTEGRIDAD y
DISPONIBILIDAD del dato y de las cifras publicadas**, no confidencialidad.

**Postura (17-ago-2026, decisión del autor):** seguridad al **mínimo viable**; el esfuerzo se dirige a la
integridad del dato (contratos, gates fail-closed, ledger, guardián de consistencia) y a la calidad del código.

## 2. Activos

| Activo | Dónde vive | Propiedad crítica |
|---|---|---|
| HTML crudo congelado | `s3://visapredictai-raw-snapshots/raw-html/` (versioning ON) + `data/snapshots/` (gitignored) | Integridad + disponibilidad (la fuente oficial pierde boletines) |
| Panel y almacén | `data/processed/` (CSV en git; DuckDB regenerable) | Integridad (base de todas las cifras) |
| Artefactos de gobernanza/evaluación y ledger prospectivo | `reports/governance/`, `reports/eval/`, `reports/prospective/` | Integridad (regla #0; evidencia de promoción append-only) |
| Release manifest | `reports/release/release_manifest.json` | Integridad (raíz de confianza del build web) |
| Identidad del repo | GitHub, rama `main`, historia single-author | Integridad + política anti-coautoría de IA |
| Credenciales | **Cero keys estáticas**: role OIDC `gh-actions-visapredict`; secret único = build hook de Netlify | Confidencialidad |

## 3. Controles que SE MANTIENEN (y dónde se verifican)

| Control | Verificación |
|---|---|
| Actions de terceros **fijadas por SHA de commit** en todos los workflows | revisión en PR (sin gate automático desde el ADR 0002) |
| **OIDC sin keys estáticas** para AWS; política del role sin `Delete` | `freeze_and_rebuild.yml` (`configure-aws-credentials` con role) |
| **Locks por perfil × plataforma** (`locks/`, `make lock`, `lockset.json`, `tools/lock_contracts.py`): CI y cron instalan bajo el lock de su perfil | `tests/test_toolchain_pin.py`; `lint-and-test`/`model-tests` en `ci.yml` |
| Hook **anti-coautoría de IA** en `commit-msg` + job `commit-policy` | `tools/check_no_coauthor.sh`, `ci.yml` |
| Parse **100 % offline** sobre snapshots congelados; validación del HTML antes de congelar | `pipeline/freeze_snapshots.py`, `tests/test_architecture.py` (puertos de red) |
| Gates duros del cron: completitud del mes, `mega_audit`, contratos de artefactos, guardián de consistencia | `tools/check_ingestion.py`, `pipeline/mega_audit.py`, `tools/check_contracts.py`, `tools/check_consistency.py` |
| **Ledger v2 fail-closed** + promoción pre-registrada ligada por identidad | `vp_model/ledger.py`, `vp_model/promotion.py`, `tests/test_promotion_gate.py` |
| **Release gate bloqueante** + deploy del web solo tras **CI verde del SHA exacto**; `release_id` content-addressed | `freeze_and_rebuild.yml` (jobs `release`, `ciwait`, deploy) |
| Heartbeat SES con DKIM + `watchdog.yml` como dead-man switch | `freeze_and_rebuild.yml`, `watchdog.yml` |
| Ruleset `main-protected` (PR + squash + `ci-gate` + historia lineal) | GitHub (ver ADR 0002 §Consecuencias sobre el cron) |

## 4. Controles RETIRADOS (ADR 0002) y riesgo residual aceptado

- Retirados: workflow `scheduled-quality` (pip-audit + SBOM), job `supply-chain` (auditor de advisories vs
  `security/python_advisories.json`), job `deep-lock-install` (instalación real de los locks deep en runners),
  guard documental `check_supply_chain_triage`, el propio `security/python_advisories.json` y `docs/SECURITY_TRIAGE.md`.
- **Riesgo residual aceptado:** dependencias con advisories conocidos pueden permanecer en los locks (en agosto de
  2026: `gitpython`, `mlflow`, `sqlparse` en el perfil `deep`; `sharp`/libvips en el web, sin fix). Justificación:
  superficie offline, sin tráfico de terceros, datos públicos. **Canal informativo sustituto:** Dependabot alerts del
  repositorio (correo al autor), sin código ni gate.
- Verificación manual de un lock deep cuando haga falta: `pip install --require-hashes -r locks/deep-linux-x86_64-cpu.txt && pip check`.

## 5. Revisión

Se revisa cuando cambie la postura (p. ej., si el repo pasa a servir tráfico, a tener más de un autor o a manejar
datos no públicos) o al retirar/añadir un control de la tabla §3. No hay SLA por severidad.
