# ADR 0005 — expediente separado para mantenimiento de una release publicada

**Estado: adoptado en local por Javier, 20-oct-2026.** Esta decisión adopta el
contrato y autoriza su implementación/ensayo local; no autoriza la publicación de un candidato.

## Frontera

La campaña de ADR 0003 permanece terminal en `published`. El mantenimiento C1 no
recalcula la ciencia ni consume otra vez su recibo `/3`. Tampoco es un boletín
mensual: no se fuerza el gate `rebuilt` del cron. El estado de ingesta se conserva
como observación histórica; un sondeo nuevo sería otro alcance.

## Contrato preparado

`python -m tools.check_maintenance_release --repo R --base SHA --candidate SHA`
produce un expediente `metadata-maintenance-inspection/1`, no un permiso.
Exige commits completos, candidato en HEAD y limpio, base ancestral del candidato,
lectura de objetos Git sin reemplazos y archivos publicados regulares `100644`.
Comprueba inventario, hashes, tamaños y la identidad normalizada del corte con el
mismo gate de contratos. El commit de origen del candidato debe contener el árbol completo salvo
los dos archivos que emite el corte, incluidos código y documentos que el RAG lee;
puede no ser ancestro del squash integrado.

Esta primera versión tiene una allowlist cerrada para C1: sólo cambian tarjeta y
manifiesto, y sólo se añade al inventario el contrato nominal de ingesta v1. El resto
de los artefactos y sus metadatos permanecen idénticos. Reportes, datos, pesos,
punteros DVC, lock DVC y pre-registro de cohortes no admiten otro cambio. Los cambios
de código y pruebas siguen requiriendo revisión del diff y CI; la igualdad de
artefactos no demuestra equivalencia semántica de cualquier código nuevo.

El expediente registra el commit/árbol candidato, release, hash del manifiesto,
commit de origen y base comparada. **No demuestra por sí mismo que la base esté
publicada**, que haya pasado CI ni que una persona haya aprobado: esas son evidencias
externas que la decisión de adopción debe ligar a estas identidades exactas.

Las opciones pareadas `--review` y `--ci` comprueban esquemas cerrados, identidad
completa del candidato, repo/workflow y los cuatro jobs verdes, corrida y orden
temporal (CI antes de revisión). La revisión declara persona y decisión aprobada;
`tested_tree_sha` identifica el checkout realmente ensayado por los jobs: no se
deduce sólo de la cabeza del evento, que puede diferir del merge sintético de una PR.
Debe ser exactamente el árbol del candidato revisado.
el verificador conserva los hashes de esas mismas lecturas y no emite una firma.
Ese cotejo opcional usa el perfil `dev` y el vocabulario de identidades automatizadas
del lector de campaña; no duplica una política más débil. La inspección sin recibos
conserva su ejecución con la biblioteca estándar y Git.
Estos JSON son declaraciones cotejadas, **no autenticación**: una captura local
debe contrastarse con GitHub y la aprobación con el autor antes de adoptar o
consumir cualquier permiso. Incluso con ambos documentos, el resultado mantiene
`authorizes_publication: false`.

## Operación adoptada

La base adoptada es `a6036ec20eaf5ecb17b1cce43fe2ecea9436a82b`, release publicada
`2026-09-87a5be76e8a1`, manifiesto SHA-256
`9460c77e053da99ee1a2a225becd8f4b018a34c62412900c9eee7f1ab471c369`.
Esta primera migración sólo incorpora el contrato nominal de ingesta y actualiza
tarjeta/manifiesto; no es una autorización genérica para otras añadas o ciencia.

`python -m tools.maintenance_release --registry R ...` ofrece `init`, `inspect`,
`register`, `consume` y `status`. El registro está fuera de Git, privado 0700/0600,
ligado a instalación, host, UID y ruta física. La autorización incluye su
`publisher_id`: copiarla a otro registro o reconstruir una identidad no la reutiliza.
Los clones/worktrees que operen en ese publicador comparten **el mismo registro**.
La custodia del registro, sus backups y del usuario del sistema es autoridad local;
no se promete resistencia a un administrador que restaure/modifique toda esa autoridad.
Cambiar de host/registro requiere una nueva aprobación humana y reconciliar la
cadena consumida; la herramienta no migra ni recupera permisos por su cuenta.

`register` obtiene el CI de GitHub con GET mediante `gh api`. Exige los cuatro jobs
verdes del intento vigente y lee en sus logs el commit/árbol realmente probado;
GitHub debe confirmar también ese árbol para el commit. No acepta capturas CI
locales como autoridad. `consume` repite estas comprobaciones, verifica el CI del
squash integrado, HEAD limpio con el árbol aprobado, `main` remoto vigente y el tag
**anotado** de la release sobre el commit de origen (no sobre el commit del corte).

La revisión `/1` conserva sus identidades exactas. Su SHA-256 debe aparecer en un
comentario JSON explícito de autorización en la PR del candidato, emitido por
`jrebull` (GitHub user id `168046724`, tipo `User`), posterior a la revisión/CI y
ligado al publicador. El cuerpo es la salida `authorization_request` de `inspect
--review`: esquema `metadata-maintenance-authorization/1`, decisión
`autorizar-consumo-mantenimiento`, `publisher_id` y `review_sha256`. Es una solicitud,
no una firma. La herramienta sólo lee el comentario; nunca lo crea. Antes de
registrar/consumir contrasta autor, PR, cuerpo y tiempos por API. Editar/revocar el
comentario o cambiar CI invalida la evidencia registrada.

El acta tiene esquema cerrado, revisión y cadena de eventos bajo `flock`, CAS de
bytes y promoción atómica con fsync. `approved` admite una revisión posterior del
**mismo** candidato; `consumed` es terminal. Repetir la misma inscripción es un
no-op explícito y repetir el consumo falla. El registro no se versiona en la release,
por lo que no introduce un ciclo de hashes.

**Consumir no es desplegar.** El corte ya llegó a Git por su integración autorizada;
como los punteros DVC son idénticos a la base publicada, no hay un nuevo `dvc push`.
La operación sólo registra su aceptación propia. No hace push, tags, commits, POST
a GitHub, llamadas a Netlify ni escribe `campaign.json`. Verificar el consumo web
y producción sigue siendo un paso separado autorizado; el acta no cierra D5/F2.
La escritura durable de `consumed` es el efecto de la operación: si falla el fsync
posterior al rename se informa incertidumbre y se consulta `status` antes de
reintentar. No se declara atomicidad entre servicios ni un despliegue que no ocurrió.

## Secuencia para ejecutar

1. Aprobar este alcance y la base publicada con su evidencia remota. Publicar los
   candidatos por PR autorizado y medir CI remoto verde sobre los commits exactos.
2. Revisar el expediente y el diff completo. La aprobación humana debe identificar
   base, candidato/árbol, manifiesto, release y commit de origen. Si cambia cualquiera,
   repetir CI/revisión; el informe local no inventa esa firma.
3. Autorizar e integrar el árbol exacto; mantener el tag anotado en el commit de
   origen, sin mover tags existentes. Verificar el CI del squash y la igualdad de árbol.
4. Ejecutar `consume` con el CI de main y el registro aprobado. Conservar el acta
   consumida en la custodia del publicador. Nunca escribir `campaign.json`.
5. Consumir en web y verificar dominio, alias y permalink, hashes de artefactos,
   tarjeta del RAG y estado de ingesta. Hasta entonces, no declarar D5/F2 publicados.

El publicador de campaña conserva su `/3`, CAS, lock, inventario y remotos de
prueba. La operación de mantenimiento tiene acta y autoridad independientes;
ninguna puede usar el permiso de la otra. La orden de adopción no fabrica una
revisión ni un comentario aprobatorio para el candidato local nuevo.

Referencias de las consultas de sólo lectura:
[workflow runs](https://docs.github.com/en/rest/actions/workflow-runs),
[jobs y logs](https://docs.github.com/en/rest/actions/workflow-jobs),
[comentarios](https://docs.github.com/en/rest/issues/comments).

## Intérpretes y clon limpio

`sync_all --publish` usa `VP_PUBLISH_PYTHON` (por defecto `ante/bin/python`, perfil
`model` de los locks); la preparación usa `VP_TRACKING_PYTHON` (por defecto
`ante_nf/bin/python`, entorno MLflow). El preflight importa los lectores reales o
el sincronizador antes de efectos. Un intérprete incompleto bloquea con rc 12.
Esto no instala paquetes, no convierte dev en model y no cambia locks existentes.

Las pruebas que consumen TeX pueden omitir su módulo sólo con
`VP_ALLOW_MISSING_LATEX=1` y un hermano realmente ausente. Un documental presente
pero incompleto/ilegible falla; el CI mantiene el checkout documental y las
aserciones cross-repo. Esa variable no es una exención del guardián de consistencia.
