# ADR 0005 — expediente separado para mantenimiento de una release publicada

**Estado: propuesta comprobable en local; pendiente de adopción humana.**

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

## Secuencia propuesta para adoptar y ejecutar

1. Aprobar este alcance y la base publicada con su evidencia remota. Publicar los
   candidatos por PR autorizado y medir CI remoto verde sobre los commits exactos.
2. Revisar el expediente y el diff completo. La aprobación humana debe identificar
   base, candidato/árbol, manifiesto, release y commit de origen. Si cambia cualquiera,
   repetir CI/revisión; el informe local no inventa esa firma.
3. Autorizar e integrar el árbol exacto; mantener el tag anotado en el commit de
   origen, sin mover tags existentes. Verificar el CI del squash y la igualdad de árbol.
4. La operación autorizada de mantenimiento debe tener su registro propio, ligado
   al permiso y la release, con serialización y consumo único; nunca escribir
   `campaign.json`. No se habilita ese publicador en este ADR propuesto.
5. Consumir en web y verificar dominio, alias y permalink, hashes de artefactos,
   tarjeta del RAG y estado de ingesta. Hasta entonces, no declarar D5/F2 publicados.

La implementación deliberadamente carece de una operación `publish`: evita añadir
un atajo antes de adoptar el contrato. El ensayo del publicador de campaña sigue
usando su `/3`, CAS, lock, inventario y remotos Git/DVC de prueba. No acredita la
ejecución de un futuro publicador de mantenimiento.

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
