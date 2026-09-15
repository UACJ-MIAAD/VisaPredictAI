# ADR 0003 — La campaña como transacción con máquina de estados

- **Estado:** Aceptada y **adoptada** el 2026-09-10 (M73); antes estaba aceptada con adopción
  pendiente.
- **Fecha:** 2026-09-05 (la implementación que documenta es del 13-jul-2026, ronda 10 de auditoría).
- **Implementación:** `tools/campaign_state.py` (la máquina) y `tools/campaign_txn.py` (la
  envoltura reutilizable que la conduce desde un runner, por CLI o por gestor de contexto).
- **Verificación mecánica:** `tests/test_campaign_state.py` (18 pruebas, cada una la reproducción
  de un falso verde de la ronda 9), más `tests/test_campaign_hashing.py`,
  `tests/test_campaign_manifest.py`, `tests/test_campaign_completeness.py` y —desde M73—
  `tests/test_campaign_txn_m73.py`, que conduce **lanes sintéticos en un directorio temporal**
  a través de fallo intermedio, `SIGKILL`, `SIGTERM`, concurrencia y la prohibición de publicar.
- **Índice:** [`docs/ENGINEERING.md`](../ENGINEERING.md).

## Contexto

Una campaña de modelado dura horas, escribe decenas de artefactos y termina decidiendo si una
receta se publica. Sin una noción explícita de transacción, «terminó» era un juicio humano sobre
un directorio: un `SIGKILL` a mitad, una carrera entre dos procesos o un `printf` a medias dejaban
un estado que *parecía* completo. La ronda 9 de auditoría reprodujo varios de esos falsos verdes.

## Decisión

Una campaña es **una transacción con identidad y estado únicos** en `campaign.json`
(`schema_version` 2), gobernada por una máquina de estados explícita:

```
running --éxito técnico--> computed --gates + revision--> validated --publish--> published
   │                          │
   └──fallo o interrupción────┴────────────────────────────> failed
```

Reglas que el código impone hoy, y solo esas:

- **Solo `validated` autoriza publicar.** `running`, `computed` y `failed` bloquean, y un
  `SIGKILL` deja `running`, que también bloquea. Un estado terminal (`failed`, `published`)
  nunca retrocede.
- **Escritura atómica siempre:** temporal en el mismo directorio, `fsync` del archivo,
  `os.replace` y `fsync` del directorio. Nunca `printf`.
- **Serialización por `flock` exclusivo** en cada transición, que además **relee** el archivo y
  exige el estado y la `revision` esperados antes de escribir: una carrera `failed`/`computed`
  no puede pisar a un terminal.
- **Arranque `O_EXCL`:** sellar una campaña que ya existe aborta. No hay «reiniciar encima».
- **Esquema estricto:** SHA de git de 40 hex, hashes `sha256:` de 64, marcas de tiempo RFC 3339
  con zona, `campaign_id` no vacío, `git_dirty` booleano exacto sin coerción, y se rechazan
  tanto las claves desconocidas como las duplicadas del JSON.
- **Invariantes por estado:** no se llega a `computed` ni a `validated` con gates, revisor,
  recibo o marcas de tiempo en `null`. ⚠️ Hasta M73 esto sólo lo imponían los argumentos de
  `mark_*`, es decir el camino de ESCRITURA: un `campaign.json` escrito por cualquier otra vía
  podía declararse `validated` con todo en `null` y pasar `validate_schema`. Al adoptar la
  máquina apareció un lector que decide si publicar, así que la invariante pasó a exigirse
  también **al leer**, y la afirmación de este ADR es ahora cierta en ambos caminos.

## Cómo se conduce (M73)

`tools/campaign_txn.py` es la **única** envoltura, y sirve a los dos tipos de consumidor: por
subcomandos (`open` · `compute` · `fail` · `validate` · `publish` · `guard` · `archive` · `status`)
para los runners de shell, y por el gestor de contexto `campaign()` para los de Python, que marca
`failed` si el cuerpo lanza. Sin una envoltura única, cada runner habría acabado con su propia
secuencia de llamadas y su propio criterio ante un estado terminal.

- **`experiments/run_rederivation.sh`** —el runbook canónico de la re-derivación— archiva la
  transacción anterior **sólo si terminó**, sella la nueva, instala un `trap` que registra
  cualquier salida anormal (incluidas señales) y llega a `computed` con las puertas de entrada y
  salida en `passed`; la consistencia puede quedar en `pending` —cómputo completo, cifras por
  propagar— y es `validate` quien vuelve a exigirla re-ejecutando el guardián.
- **`experiments/sync_all.sh --publish`** consulta la transacción antes de cada `dvc push` y cada
  `git push`, y **consume el permiso** con `txn publish` después del push. Es la **segunda**
  puerta: el manifiesto de campaña acredita la *identidad*, la transacción acredita el *estado*.
- **`validate` no acepta revisor ni decisión por argumento** (M74-B-R1). Los lee de un **recibo
  JSON de esquema cerrado** (`campaign-validation-receipt/2`, ocho claves exactas) ligado a la
  campaña por `campaign_id`, SHA de origen, `panel_sha256` e `input_seal_sha256` (M74-E-R12), con decisión de vocabulario cerrado,
  revisor que no puede declararse automatizado y fecha posterior al cómputo que revisa. **No hay
  bandera para saltarse el guardián de consistencia**: la que había era un bypass de producción.
- Relanzar sobre una campaña abierta **aborta**: `seal_running` es create-only y el archivado
  exige un estado terminal.

## Lo que esta decisión NO garantiza

Se documenta lo que el código hace, no lo que sería deseable:

- No hay reversión de artefactos. La transacción gobierna **el permiso para publicar**, no
  deshace ficheros ya escritos por la campaña.
- No hay bloqueo entre máquinas: `flock` es local al sistema de archivos.
- No cubre el `cron` mensual, que tiene su propia cadena de gates (release gate, CI del SHA
  exacto, `tools/cron_publish.py`), descrita en [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md) y
  [`docs/FAILURE_MATRIX.md`](../FAILURE_MATRIX.md).
- **No hay estado para «técnicamente completa, con cifras por propagar».** El enum no lo tiene y
  M73 no lo inventó: `mark_computed` exige las tres puertas en `passed`, así que una campaña
  cuya consistencia queda rota se registra como `failed` con una razón que dice literalmente
  que el cómputo terminó y lo que falta es propagar. El nombre del estado es impreciso; el
  efecto —no se publica— es el correcto. La alternativa, dejarla en `running`, la volvería
  indistinguible de una campaña muerta a mitad.
- **`guard` sólo protege a quien lo llama.** La envoltura no puede interceptar una publicación
  que no pase por ella; hoy el único publicador de campaña es `sync_all.sh`.

## Consecuencias

Desde M73 la garantía **es práctica y no sólo biblioteca**: el runbook canónico conduce la
transacción y el publicador la consulta. Lo que sigue sin cubrir se dice arriba, y
`tests/test_docs_links.py` vigila ahora la afirmación contraria a la de antes — si el último
runner dejara de conducirla, la prueba falla y este texto tendría que volver a decir que la
máquina no se usa.

Los demás `run_campaign*.sh` siguen **sin** conducirla: son los orquestadores que el runbook
canónico invoca como etapas, y sellar una transacción por etapa produciría varias identidades para
una sola campaña, que es exactamente el defecto que la máquina evita.

## Alternativas descartadas

- **Marcador de éxito en disco** (`DONE` vacío): no distingue interrupción de fallo, no
  serializa y no lleva identidad ni procedencia.
- **Estado en la base de datos del tracking**: acopla la decisión de publicar a un servicio que
  el proyecto declaró archivo histórico y no dashboard vivo (ver
  [`docs/mlops_experimentos.md`](../mlops_experimentos.md)).
