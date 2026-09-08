# Runbook · propagación DATA → WEB → PROD

[← Índice normativo](ENGINEERING.md)

> **El orden no es una costumbre.** El job `consistency` del repo de datos valida contra la rama
> por defecto del repo WEB: una regla nueva que prohíba algo que el web todavía dice deja el PR de
> datos **rojo por construcción**. Y el sitio no queda `fresh` hasta que el manifiesto vive en
> `main` de datos. Se aprendió a golpes (M33) y por eso está escrito.

## Orden obligatorio

1. **DATA** — el cambio de datos entra a `main` con su CI verde.
2. **WEB** — el sitio consume el corte nuevo y aterriza después.
3. **PROD** — la publicación automática de Netlify sirve el corte; se verifica **leyendo la URL**.

**La única inversión legítima** es cuando el guardián demuestra que un consumidor del web
todavía dice lo que la regla nueva prohíbe: entonces **el web aterriza primero** y el gate de datos
después. Nunca se debilita la regla para conservar el orden.

## Precondiciones (`make propagate-check`)

Comando de **solo lectura**: no modifica, no regenera, no descarga, no pushea y no despliega.
Comprueba, y falla cerrado ante cualquiera:

| # | Precondición | Por qué |
|---|---|---|
| 1 | Ambos árboles limpios | propagar desde un árbol sucio publica algo que nadie revisó |
| 2 | Manifiesto legible y con artefactos `critical` | sin autoridad no hay nada que propagar |
| 3 | Cada artefacto `critical` presente y con su **sha256** | un artefacto manipulado no se publica |
| 4 | El pin del web coincide con el `release_id` del manifiesto | pin divergente = datos y web describen cortes distintos |
| 5 | El corte que el web declara **existe y es ancestro** de `HEAD` en datos | si el web va por delante, se propagó al revés |

El repo web se localiza por `VP_WEB_DIR` o por la convención de directorio hermano. **Ninguna ruta
personal vive en el código.**

## Condiciones de parada

- **Cualquier rojo de CI.** Sin rerun, salvo fallo externo demostrado por el log completo (un
  `FinalizeArtifact 403` o un timeout de red en `npm ci` lo son; una prueba roja no).
- **Conflicto con un artefacto sellado**: si el cambio obligaría a regenerar el manifiesto, la
  tarjeta o el `release_id` que sirve producción, se detiene y se decide aparte.
- **Precondición 4 o 5 en rojo**: el orden ya se rompió; no se sigue hacia PROD.

## Verificación

- **DATA**: CI de push 5/5 sobre el squash; `consistency` y `contracts` en verde.
- **WEB**: los cuatro checks y el *deploy preview*; se lee el preview en **ES y EN** antes del
  merge.
- **PROD**: se lee la URL, no el log del deploy — `/data/release-state.json` (`status`,
  `release_id`) y las páginas afectadas en ambas lenguas. Netlify **no** publica commit status:
  la prueba del deploy es el `fetched_at` del estado servido, no un check de GitHub.

## Rollback

1. **Nada se fuerza.** No hay `--force`, ni `--admin`, ni reescritura de `main`.
2. Si el corte publicado es malo, se **avanza** con un commit nuevo (el flujo normal), porque el
   sitio sirve el último `main` del web y el último manifiesto de datos.
3. Si hay que volver a un corte anterior de datos, se hace por PR revirtiendo el commit, con su CI;
   el web vuelve a apuntar al corte anterior en su propio PR, **en ese orden**.
4. La rama de trabajo se conserva hasta el cierre administrativo: bundle por ref, equivalencia por
   `patch-id`, `merge --ff-only` y borrado por **CAS**. Un bundle verificado es lo que permite
   deshacer sin depender de la copia remota.
