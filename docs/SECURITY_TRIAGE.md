# Triage de dependencias Python y política de SLA (G5)

Gate programado: `.github/workflows/scheduled-quality.yml` (lunes + dispatch) corre
`pip-audit` sobre los **locks versionados** — runtime/dev VETAN (dependencias directas:
se arreglan por PR con tests y rollback), model-cpu (232 paquetes del entorno de
referencia) se reporta y se tría aquí. SBOM CycloneDX del perfil dev como artefacto por
corrida. Misma política de SLA que el repo web (`VisaPredictAI_web/docs/SECURITY_TRIAGE.md`):
critical 48 h · high 7 días · moderate 30 días o el bump del upstream directo · low con
el siguiente upgrade. **Prohibido el auto-fix**: todo bump va por PR con suite verde.

## Resuelto al estrenar la política (2026-07-11)

- **requests 2.32.5 → 2.33.0** (CVE-2026-25645, runtime+dev): dependencia DIRECTA con
  fix disponible → actualizada en el acto (pyproject + locks regenerados de venvs
  frescos + suite de parsers/extracción verde — el único consumidor es la capa de
  scraping). `pip-audit` de runtime/dev: **limpio**.

## Triage vigente — perfil model-cpu (entorno de referencia)

| Paquete | Advisory | Dónde corre | Explotabilidad | Acción |
|---|---|---|---|---|
| pypdf 6.13.2 | GHSA-jm82-fx9c-mx94 (fix 6.13.3) | Solo herramientas locales de extracción de PDFs de revisión (confiables, del director) | BAJA (input confiable) | Bump con el siguiente `make lock` deliberado |
| pytorch-lightning 2.5.6 | CVE-2026-31221 (sin fix listado) | Entrenamiento local/campañas; no corre en el cron de datos | BAJA (sin superficie remota) | Vigilar release del upstream; SLA moderate |
| pydantic-settings 2.14.1 | GHSA-4xgf-cpjx-pc3j (fix 2.14.2) | Transitiva de la pila de modelado | BAJA | Bump en el siguiente refresh del perfil |
| msgpack 1.2.0 | GHSA-6v7p-g79w-8964 (fix 1.2.1) | Transitiva (serialización local) | BAJA (sin datos no confiables) | Ídem |

**Acciones/contenedores/locks (aceptación G5):** todas las Actions de ambos repos van
pinneadas por SHA; la imagen del gate LaTeX es la única "contenedor" (texlive/texlive,
job de verificación sin secretos); los locks por perfil son la fuente del audit y del
SBOM. El repo web cubre su mitad en G2 (npm audit semanal + triage propio).
