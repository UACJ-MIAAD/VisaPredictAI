# ADR 0003 — La campaña como transacción con máquina de estados

- **Estado:** Aceptada, con adopción pendiente (ver *Consecuencias*).
- **Fecha:** 2026-09-05 (la implementación que documenta es del 13-jul-2026, ronda 10 de auditoría).
- **Implementación:** `tools/campaign_state.py`.
- **Verificación mecánica:** `tests/test_campaign_state.py` (18 pruebas, cada una la reproducción
  de un falso verde de la ronda 9), más `tests/test_campaign_hashing.py`,
  `tests/test_campaign_manifest.py` y `tests/test_campaign_completeness.py`.
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
  recibo o marcas de tiempo en `null`.

## Lo que esta decisión NO garantiza

Se documenta lo que el código hace, no lo que sería deseable:

- No hay reversión de artefactos. La transacción gobierna **el permiso para publicar**, no
  deshace ficheros ya escritos por la campaña.
- No hay bloqueo entre máquinas: `flock` es local al sistema de archivos.
- No cubre el `cron` mensual, que tiene su propia cadena de gates (release gate, CI del SHA
  exacto, `tools/cron_publish.py`), descrita en [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md) y
  [`docs/FAILURE_MATRIX.md`](../FAILURE_MATRIX.md).

## Consecuencias

La máquina está implementada y probada, pero **ningún runner de `experiments/` la conduce
todavía**: los `run_campaign*.sh` actuales no sellan ni transicionan `campaign.json`. Es decir,
la garantía existe como biblioteca y no como práctica. Adoptarla es trabajo posterior y con su
propia autorización; hasta entonces este ADR describe una capacidad disponible, no un control
activo, y `tests/test_docs_links.py` falla si algún runner empieza a usarla sin que el texto se
corrija.

## Alternativas descartadas

- **Marcador de éxito en disco** (`DONE` vacío): no distingue interrupción de fallo, no
  serializa y no lleva identidad ni procedencia.
- **Estado en la base de datos del tracking**: acopla la decisión de publicar a un servicio que
  el proyecto declaró archivo histórico y no dashboard vivo (ver
  [`docs/mlops_experimentos.md`](../mlops_experimentos.md)).
