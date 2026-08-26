# ADR 0002 — Retiro de los gates de cadena de suministro; seguridad al mínimo viable

- **Estado:** Aceptada.
- **Fecha:** 2026-08-26.
- **Origen:** decisión del autor del 17-ago-2026 (*"deja de enfocarte en seguridad… enfócate en calidad del repo, clean
  code, SOLID y MLOps"*), formalizada el 26-ago-2026 (Gate Q) en `Prompts/PLAN_MLOPS_V2_AUDITORIA_2026-08-26.md`.
- **Verificación mecánica:** `tests/test_toolchain_pin.py` (los locks siguen gobernando CI/cron), `ci.yml` con
  `ci-gate.needs = [commit-policy, lint-and-test, model-tests, consistency]`, `tools/validate_structure.sh`.

## Contexto

Entre julio y agosto de 2026 el repositorio acumuló un gate de cadena de suministro en tres capas: un workflow semanal
(`scheduled-quality.yml`: `pip-audit` sobre 11 locks + SBOM), un job de CI en cada PR/push (`supply-chain`: auditor
`tools/audit_python_supply_chain.py` reconciliando los locks contra `security/python_advisories.json` con biyección
exacta y excepciones con vencimiento) y una instalación real de los locks deep en runners Linux y macOS
(`deep-lock-install` + `tools/deep_smoke.py`), más un guard documental (`tools/check_supply_chain_triage.py` sobre
`docs/SECURITY_TRIAGE.md`/`docs/THREAT_MODEL.md`).

Desde el 10-ago-2026 ese gate está **rojo por construcción**: aparecieron advisories nuevos en dependencias del perfil
`deep` (`gitpython`, `mlflow`, `sqlparse`) y venció una excepción (`PYSEC-2026-3043`, 2026-08-12). Como el ruleset
`main-protected` exige `ci-gate` verde para mergear, **ningún PR podía entrar a `main`** — incluidos los que no tocan
dependencias. El modelo de amenaza real del proyecto (repositorio académico, un autor, datos públicos, sin servicio
expuesto) no justifica sostener esa carrera de bumps.

## Decisión

1. **Se retiran** `scheduled-quality.yml`, los jobs `supply-chain` y `deep-lock-install`, el paso
   `Check docs ↔ advisories JSON`, los módulos `tools/audit_python_supply_chain.py`, `tools/check_supply_chain_triage.py`,
   `tools/deep_smoke.py` con sus tests, el directorio `security/` y `docs/SECURITY_TRIAGE.md`. `ci-gate` pasa a depender
   de `[commit-policy, lint-and-test, model-tests, consistency]` (el nombre del check requerido **no cambia**).
2. **Se conserva íntegra la cadena de reproducibilidad de entornos:** `locks/` (9 locks + `lockset.json`),
   `tools/make_locks.sh`, `tools/promote_lockset.py`, `tools/lock_contracts.py`, `requirements/deep*.in` y el guard
   `tests/test_toolchain_pin.py`. CI y el cron siguen instalando bajo el lock de su perfil.
3. **Se conserva el mínimo viable de seguridad:** cero keys estáticas (OIDC), actions fijadas por SHA, hook y job
   anti-coautoría de IA, ruleset `main-protected`.
4. **Canal informativo sustituto:** Dependabot alerts del repositorio (sin código, sin gate). Verificación manual de un
   lock deep cuando haga falta: `pip install --require-hashes -r locks/<perfil>.txt && pip check`.
5. `docs/THREAT_MODEL.md` se reescribe (v2) para reflejar los controles vigentes y el riesgo residual aceptado.

## Consecuencias

- **Positivas:** `ci-gate` vuelve a ser verde cuando el código lo es; −4 a −5 minutos de runner por corrida y ningún
  runner macOS; `tools/` pasa de 4,157 a 3,475 líneas / 25→22 archivos (0.32× el producto de 10,879 L; medido con `git ls-tree`); desaparece la deuda de excepciones con
  vencimiento.
- **Negativas (aceptadas):** dependencias con advisories conocidos pueden permanecer en los locks; nadie instala los locks
  deep en CI (su reproducibilidad se prueba a mano o al correr una campaña).
- **Sobre el cron:** el ruleset `main-protected` (sin `bypass_actors`) impide *cualquier* push directo a `main`, también el
  del cron `freeze_and_rebuild`. Habilitar la publicación automática requiere una decisión aparte del autor (bypass para
  la app GitHub Actions o cron por PR); este ADR no la toma.
- **Reversión:** los módulos retirados quedan en la historia (`git log -- tools/audit_python_supply_chain.py`); re-activar
  el gate es restaurarlos y volver a listar los jobs en `ci-gate.needs`.
