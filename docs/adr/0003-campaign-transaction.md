# ADR 0003 — La campaña como transacción con máquina de estados

- **Estado:** Aceptada y **adoptada** el 2026-09-10 (M73); antes estaba aceptada con adopción
  pendiente.
- **Fecha:** 2026-09-05 (la implementación que documenta es del 13-jul-2026, ronda 10 de auditoría).
- **Implementación:** `tools/campaign_state.py` (la máquina) y `tools/campaign_txn.py` (la
  envoltura reutilizable que la conduce desde un runner, por CLI o por gestor de contexto). El
  **esquema** de `campaign.json` vive desde G7 en `vp_model/campaign_schema.py`, la única autoridad:
  la máquina lo reexporta y los lectores de `vp_model` que prestan la identidad de la campaña
  (`artifact_receipt.read_running_transaction`, el camino de campaña de los consumidores de CRPS)
  lo exigen al leer (ADR-0001 impide que `vp_model` importe `tools`). La transición a `published`
  no es una API pública de la máquina: su única entrada es `campaign_txn.publish`, que pasa la
  puerta (`publishable`) y escribe con CAS sobre la revisión que acreditó.
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
subcomandos (`open` · `compute` · `fail` · `validate` · `revalidate` · `publish` · `publish-release` · `guard` ·
`archive` · `status`)
para los runners de shell, y por el gestor de contexto `campaign()` para los de Python, que marca
`failed` si el cuerpo lanza. Sin una envoltura única, cada runner habría acabado con su propia
secuencia de llamadas y su propio criterio ante un estado terminal.

- **`experiments/run_rederivation.sh`** —el runbook canónico de la re-derivación— archiva la
  transacción anterior **sólo si terminó**, sella la nueva, instala un `trap` que registra
  cualquier salida anormal (incluidas señales) y llega a `computed` con las puertas de entrada y
  salida en `passed`; la consistencia puede quedar en `pending` —cómputo completo, cifras por
  propagar— y es `validate` quien vuelve a exigirla re-ejecutando el guardián.
- **`experiments/sync_all.sh --publish`** (desde G7 · desbloqueo) **no prepara nada**: no sincroniza MLflow, no
  re-hashea, no commitea ni empuja git. Consume el permiso de la release YA integrada que hay en `HEAD` llamando a
  `campaign_txn publish-release` (ver § Publicar la release aprobada). Es la **segunda** puerta: el manifiesto de
  campaña acredita la *identidad*, la transacción acredita el *estado*. Preparar (MLflow, `dvc add`, punteros
  staged) es su modo por defecto y ocurre ANTES del candidato y de la firma.
  Desde M74-E-R13 `guard` y `publish` exigen `--manifest`: `publishable()` acredita el sello del manifiesto y
  cruza su identidad con la transacción sobre una sola lectura, y `publish` lo repite justo antes de escribir.
- **`validate` no acepta revisor ni decisión por argumento** (M74-B-R1). Los lee de un **recibo
  JSON de esquema cerrado** (`campaign-validation-receipt/2`, ocho claves exactas) ligado a la
  campaña por `campaign_id`, SHA de origen, `panel_sha256` e `input_seal_sha256` (M74-E-R12), con decisión de vocabulario cerrado,
  revisor que no puede declararse automatizado y fecha posterior al cómputo que revisa. **No hay
  bandera para saltarse el guardián de consistencia**: la que había era un bypass de producción.
  Desde M74-F · G7 · B5a ese recibo ya no autoriza nada por sí solo: `validate` y la puerta exigen
  el `/3`, que añade el vínculo con el resultado (ver § Publicar una campaña reconciliada).
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
  que no pase por ella; hoy el único publicador de campaña es `sync_all.sh --publish` → `publish-release`.

## Complementarias y reconciliación (M74-F · G7)

Una campaña ya `computed` no vuelve a producir: los huecos que deja se cierran con transacciones
**complementarias** (`vp_model/complement.py`), cada una en su propio worktree desechable, que leen
la madre sin escribirla y no pasan nunca por `campaign.json`. Para llevar lo que acreditan a los
canónicos, el **recibo de reconciliación** (`vp_model/reconciliation.py`, CLI
`experiments/run_reconciliation.py`) junta en un árbol más las salidas de la madre, extraídas de su
tar sellado, y los artefactos de cada complementaria `accredited`, re-acreditada con su propio
lector. Sella también el inventario de `reports/`. Los consumidores de CRPS aceptan esa
reconciliación como transacción del árbol y la vuelven a acreditar al leer. `derived` no autoriza
propagar ni publicar: la publicación sigue siendo sólo `campaign_txn.publish` sobre la madre.

## Publicar una campaña reconciliada (M74-F · G7 · B5a)

Publicar siguió siendo sólo `campaign_txn.publish` sobre la madre, pero validarla reescribía su
`campaign.json`, cuyo hash fijan complementarias, reconciliaciones y evidencia copiada: validar la
madre las dejaba a todas sin acreditar. Ahora se separan las dos cosas que ese archivo contaba:

- **La evidencia del cómputo es inmutable.** Al pasar a `validated`, la máquina guarda bajo su
  lock una copia create-only y exacta del `campaign.json` en `computed` (`campaign.computed.json`)
  y registra su hash en `computed_sha256`. El lector único (`vp_model/campaign_evidence.py`)
  devuelve ese hash para `computed`, `validated` y `published`, y sólo si el estado vivo describe
  el mismo cómputo campo a campo. El hash original se conserva; nada se compara por `campaign_id`.
- **La autorización se liga al resultado exacto.** El recibo `campaign-validation-receipt/3`
  (`vp_model/vinculo_publicacion.py`) nombra la evidencia de la madre, la reconciliación (raíz, id
  y hash de su `reconciliation.json`, que sella fuentes, capas y salidas) y el candidato de release
  (raíz, commit, árbol, manifiesto y `release_id`). `validate`, `guard` y `publish` lo re-acreditan
  en vivo: la reconciliación con su propio lector, el candidato limpio de verdad (no el `git_dirty`
  sellado) y cada reporte versionado igual al de la reconciliación salvo el manifiesto y la tarjeta
  del corte.
- **Se publica sólo el árbol revisado.** `publish` exige además que el commit publicado tenga
  exactamente el árbol del recibo; si no, se niega y el permiso no se consume.
- **Los bytes revisados son los del commit, no los del disco** (B5a-3). Reportes, manifiesto y
  tarjeta del corte se leen del commit revisado con su modo y su blob (`git ls-tree` +
  `git cat-file`). Sólo se publica un archivo regular (`100644`): un enlace simbólico guardaría una
  ruta y no los bytes a los que apunta. Falta un archivo del corte o no se puede leer un blob:
  se bloquea. El hash y el `release_id` del manifiesto salen de los mismos bytes. Ninguna consulta
  al candidato sigue reemplazos locales (`git --no-replace-objects`, también en la comprobación del
  árbol de `publish`): un `refs/replace` haría leer otro commit, árbol o blob.

- **Una sola lectura del recibo.** `validate` acredita los bytes que leyó y registra el hash de esos
  mismos bytes; la puerta los lee una vez y `publish` toma de esa misma lectura el árbol exigido.
  Cambiar el archivo entre la comprobación y el uso ya no cambia lo que se publica.
- **El candidato se coteja con un contrato de exportación explícito.** `EXPORTACION` nombra cada
  salida de la reconciliación que no viaja al repositorio de datos (las figuras re-derivadas van al
  documental; las de cohortes se quedan locales). Todo lo demás del inventario sellado es
  obligatorio en el candidato, y una salida del contrato que aparezca en datos sobra. Las reglas de
  `.gitignore` o `.git/info/exclude` no deciden qué evidencia es obligatoria. El vínculo no verifica
  el lado documental de ese contrato.

El recibo `/2` **ya no autoriza** validar ni publicar. Una campaña sin complementarias sigue la
misma vía: una reconciliación con cero fuentes (`run_reconciliation.py` sin `--source`) y un
recibo `/3`.

## Publicar la release aprobada (G7 · desbloqueo)

Antes, `sync_all --publish` commiteaba los punteros, hacía `dvc push` y `git push`, y **después** `txn publish`
comprobaba el árbol aprobado: con un recibo `/3` el permiso no se consumía nunca, pero el commit ya estaba en el remoto.
Ahora preparar y publicar son materialmente distintos:

1. **Preparar** (por defecto): MLflow, `dvc add`, punteros staged. Cambia el árbol, así que entra al candidato ANTES de
   sellar el corte, del CI y de la firma.
2. **Integrar**: la PR del candidato entra a `main` con EXACTAMENTE el árbol firmado (merge o squash que lo reproduzca).
3. **Publicar** (`sync_all.sh --publish` → `campaign_txn publish-release`): en un checkout limpio cuyo `HEAD` es la
   release integrada. Bajo el lock de la campaña —serializado frente a `revalidate`, `fail` y `validate`— y ANTES del
   primer efecto externo se comprueban la puerta completa, el árbol de `HEAD`, el checkout limpio y que `HEAD` esté en
   `origin/main`. Después, el único efecto externo: `dvc push` con el DVC gobernado (direccionado por contenido). Sólo si
   salió bien, `published` con CAS sobre la revisión y la huella acreditadas.

Punto de no retorno: si `dvc push` termina y la escritura de `published` falla, los objetos ya están en el remoto
(inocuo) y la campaña sigue `validated`; reintentar es seguro. Si `dvc push` falla, no salió nada nuevo y el permiso no
se consumió. Una release sin punteros nuevos se publica igual (`dvc push` no sube nada): no se fuerza un commit ni se
devuelve verde sin consumir el permiso; repetir la publicación no está autorizado porque la campaña ya es `published`.
No se promete atomicidad entre DVC, git, el sistema de archivos y el hosting. Ensayo: `tests/test_g7_publicador_ensayo.py`
(remoto git bare, remoto DVC local, `sync_all.sh` y `dvc` reales).

## Revalidar una madre validada (G7 · desbloqueo)

El recibo `/3` liga un árbol **exacto**. Si el candidato tiene que cambiar después de la firma —por ejemplo, para que el
CI pase—, la madre `validated` sólo podía ir a `published` con el árbol viejo o a `failed`, perdiendo el cómputo. La
revalidación es una operación **explícita** (`campaign_txn revalidate --receipt <nuevo /3>` → `campaign_state.revalidate`),
no una arista genérica: `_ALLOWED["validated"]` sigue siendo `{published, failed}`.

- **Sólo `validated`.** `published` y `failed` siguen terminales; no se vuelve a `computed` ni se re-sella el cómputo:
  la evidencia (`campaign.computed.json`) se re-acredita y `computed_sha256` no cambia.
- **Un `/3` nuevo,** acreditado igual que en `validate` (consistencia, esquema cerrado, misma campaña, decisión
  `aprobada`, revisor humano, vínculo vivo con madre, reconciliación y candidato) y además **posterior** al permiso que
  sustituye y **distinto** (ni el mismo sha256 ni la misma ruta).
- **El permiso anterior se conserva** en `campaign.validations/`, CREATE-ONLY y atómico (temporal + `link`): la copia
  exacta del recibo sustituido (`rNNNN.recibo.json`), la del estado sustituido (`rNNNN.campaign.json`) y el **registro**
  `rNNNN-rMMMM.revalidation.json` que los encadena (huella del estado anterior, recibo anterior y nuevo, revisor, fecha).
  El registro es el punto de compromiso y se escribe **antes** de promocionar el estado, bajo el lock de la campaña.
- **CAS sobre la revisión y sobre la huella de los bytes acreditados.** Una reescritura que conserva `revision` también
  se rechaza (la misma regla protege ahora a `publish`). La revisión sube exactamente una vez.
- **La puerta acredita la historia entera.** Con registros, forman una cadena consecutiva que empieza en la validación
  original (revisión del cómputo + 1) y termina en el estado vivo, y cada eslabón conserva su estado y su recibo. Sin
  registros, el estado debe seguir en la validación original. Un registro «preparado sin promocionar» (caída entre
  registro y estado), un registro borrado o alterado, o un estado devuelto a mano a una revisión anterior **cierran la
  puerta**: el permiso nuevo no autoriza y el viejo no revive.
- **Recuperación.** Una caída antes del registro deja copias inertes y el permiso vigente intacto. Una caída entre
  registro y estado se completa repitiendo la revalidación con el **mismo** recibo (conserva la fecha del registro);
  otro recibo se rechaza mientras haya una revalidación preparada.

Límites: el historial vive junto a la transacción y la máquina lo protege de accidentes y de lectores que crean al
escritor, no de quien reescriba a mano el directorio entero con hashes coherentes; la fecha de la revisión humana es la
que declara el recibo.

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
