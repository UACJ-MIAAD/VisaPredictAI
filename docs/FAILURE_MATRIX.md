# Matriz de fallos: estado → señal → comportamiento → rollback (G4)

Cada modo de fallo del plan de auditoría, con la SEÑAL que lo hace visible, el
comportamiento del sistema, el rollback, y la PRUEBA que lo simula (test o gate que ya
corre — no simulacros de papel). Invariantes duras que ningún modo viola: **el ledger
jamás se pierde** (append-only, keep-first, actas git) y **jamás se publica una release
híbrida** (swap todo-o-nada por hash).

| Fallo | Señal | Comportamiento | Rollback | Prueba que lo simula |
|---|---|---|---|---|
| Raw host caído / manifiesto inasequible | Log de fetch-data + `release-state.json` (`stale`, razón explícita) | Dual-read legacy por-archivo; el corte previo queda ENTERO | Ninguno necesario (no hubo swap) | `tests/release.test.ts` (planSwap) + rollback `FETCH_LEGACY=1` ejercitado en vivo (B2) |
| Checksum inválido / push a medias (release híbrida) | `stale` + lista de artefactos bloqueantes fallidos | Veto del swap COMPLETO; staging se descarta | Corte previo intacto por construcción | `release.test.ts`: critical/required fallidos vetan; `verifyEntry` casos negativos |
| Corte con añadas mezcladas | `check_contracts` FALLA antes del manifiesto (cron 4h) y en CI | El corte NO se publica | Corregir la añada divergente y re-correr | `test_contracts.py::test_mixed_vintage_cut_fails` |
| Contrato derivado (schema que el loader no conoce) | `incompatible` en release-state; drift por hash vendored-vs-publicado | Corte no consumible; el previo queda entero | Vendorizar el contrato nuevo en el web (misma tanda) | `contracts.test.ts` (drift por hash) + `SUPPORTED_SCHEMA` |
| Cron parcial / reintento | Commits por fase + línea "Publicacion (allowlist)" en el correo SES | Reintentos idempotentes: el ledger no duplica (keep-first); extraños no viajan callados | La fase fallida re-corre; datos ya commiteados quedan | `test_ledger_v2` (idempotencia/reintento) + `test_cron_publish` |
| Modelo que no converge (p. ej. SARIMA) | Log del generador; serie ausente de la añada | La serie se OMITE del vintage del mes (documentado en el .tex §A.8); gate C2 aborta añadas <90% | La añada previa de esa serie sigue servida | Gate C2 en `generate_web_forecasts` (aborta añada mutilada) |
| LLM ausente / API caída (VisaBot) | `errStream` del proxy; respuesta extractiva | Fallback extractivo en el cliente | Automático al volver la API | ⚠️ GAP con registro (G3/SECURITY_TRIAGE): sin test dedicado — costura del F1-split futuro |
| CDN/header incorrecto (CSP, og) | `verify-build` FALLA el job build-offline | El PR no pasa; Netlify nunca lo ve | Revertir el commit | `scripts/verify-build.mjs` en CI (G1) |
| Release stale servida / deploy no aplicado | Correo SES "deploy NO verificado" (C4) + **watchdog de salud real** (lunes: vintage de PROD vs manifiesto + status fresh → issue) | Producción expone su estado en `/data/release-state.json` + footer | Re-disparar hook de Netlify; investigar loader | Paso C4 post-hook (sondea PROD) + watchdog G4 |
| `dvc.lock` desfasado | Gate E2 en CI (5 stages git-only) | El push no pasa CI | `make repro` + commitear lock | Gate E2 (detonó 5+ veces históricas — funciona) |
| Boletín HTML malformado | Correo SES + fail-soft por mes | Un mes malo no tumba los 296; `_looks_like_bulletin` + piso de links | El mes se recupera a mano a snapshots/ | `test_extraction` offline sobre fixtures |
| Cifra desalineada entre artefactos | Guardián de consistencia (CI + pre-push, 119 artefactos) | El push no pasa | Propagar regla #0 | `check_consistency` + tripwires probados con violaciones sembradas |

**Exposición de identidad (aceptación G4):** producción expone `release_id`/estado en
`/data/release-state.json` y en el footer (H3); el manifiesto content-addressed es la
contraparte del productor.
