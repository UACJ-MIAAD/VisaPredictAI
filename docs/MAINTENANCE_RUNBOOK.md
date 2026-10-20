# Mantenimiento nominal de una release publicada

Autoridad: ADR 0005. La adopción del contrato no aprueba un candidato ni permite
push, merge, tags, comentarios o deploys sin la orden correspondiente del autor.
La campaña publicada permanece terminal; no se emite ni reutiliza su `/3`.

## Una instalación, un registro

Elegir el registro durable **fuera de Git**, en el publicador designado. Todos sus
clones y worktrees comparten esa misma ruta. Inicializar una vez:

```sh
python -m tools.maintenance_release --registry /ruta/durable/maintenance_registry init
```

Conservar la identidad, ruta física, host y UID. Su `publisher_id` forma parte de
la autorización humana. El registro usa permisos 0700/0600 y no debe guardarse en
la rama ni duplicarse para sortear un permiso consumido. Backup y restauración
son custodia de estado, no una forma de restablecer `approved`. Migrar a otro
host/ruta requiere reconciliar el último estado y una nueva decisión humana.

## Preparar el candidato

1. Verificar ciencia, inventario y base adoptada. Hacer todos los cambios de código
   y documentos antes de emitir el corte. El manifiesto fija el commit de origen,
   no su propio commit futuro. Tarjeta/manifiesto son los únicos productos del corte.
2. Validar fuentes y corte; dejar ambos commits. Obtener autorización para PR/push.
3. Medir el CI remoto del candidato exacto. Los cuatro jobs deben terminar verdes
   y dejar `VP_MAINTENANCE_CHECKOUT` con el árbol realmente probado. La herramienta
   obtiene el intento vigente y los logs por GET de GitHub; no acepta una captura
   local inventada ni interpreta el SHA del evento como prueba del checkout.
4. Preparar la revisión humana de esquema `metadata-maintenance-review/1` con sus
   siete identidades, corrida CI, revisor y fecha posterior a CI. La automatización
   puede preparar un borrador, pero no emitir aprobación humana por sí misma.

## Registrar una aprobación expresa

Con el candidato en HEAD y Git limpio:

```sh
python -m tools.maintenance_release --repo /ruta/repo --registry /ruta/registro \
  inspect --candidate SHA_COMPLETO --review /ruta/revision.json
```

La salida `authorization_request` es el cuerpo JSON de la confirmación que el
autor puede publicar en la PR. **La salida no es autorización**. Su decisión
`autorizar-consumo-mantenimiento` liga revisión y publicador. El usuario GitHub
numérico autorizado debe crear ese comentario; la herramienta nunca lo publica.
Con su ID y la corrida CI, se puede registrar:

```sh
python -m tools.maintenance_release --repo /ruta/repo --registry /ruta/registro \
  register --candidate SHA_COMPLETO --review /ruta/revision.json \
  --run-id NUMERO_CI --comment-id NUMERO_COMENTARIO
```

La orden relee CI/autor/PR/cuerpo/fechas desde GitHub y guarda sus huellas. Repetir
exactamente esa inscripción es un no-op. Una revisión posterior del mismo candidato
puede sustituir la aprobada y queda en la cadena; un candidato distinto no hereda
permiso. El estado `consumed` es terminal incluso si se intenta registrar otra vez.

## Integrar y consumir

Con autorización separada: integrar el árbol exacto, crear el tag **anotado** de
la release sobre el commit de origen y comprobar el CI de main. No mover tags
existentes. En el checkout del squash limpio y de main remoto vigente:

```sh
python -m tools.maintenance_release --repo /ruta/repo --registry /ruta/registro \
  consume --release ID_RELEASE --integration-run-id CI_MAIN
```

La orden mantiene el lock durante todas las comprobaciones y el CAS. Vuelve a
acreditar revisión/CI/comentario y exige PR integrada, árbol exacto, main remoto,
CI del squash y tag sobre el origen. Sólo entonces escribe `consumed` con la
identidad del commit integrado. No hace Git/DVC push: los objetos DVC son los mismos
del corte publicado. No cambia `campaign.json` ni dispara Netlify.

Un comentario editado/revocado, CI rehecho después de la revisión, un tag ausente,
main adelantado, un checkout sucio o un registro cambiado bloquean antes del consumo.
No hay modo offline para acreditar esas autoridades de producción. Las simulaciones
de los tests inyectan autoridades sintéticas por código; no existe bandera CLI para ello.

## Escritura interrumpida y salida

```sh
python -m tools.maintenance_release --registry /ruta/registro status --release ID_RELEASE
```

Si la escritura falla antes del rename, el acta anterior queda vigente. Si falla
el fsync posterior al rename, la orden informa incertidumbre: consultar `status`
antes de reintentar. Si ya dice `consumed`, no repetir ni restaurar un backup viejo.
La cadena y el lock protegen frente a escritores cooperativos y corrupción detectada;
el administrador del publicador y su política de restauración siguen siendo autoridad.

El acta acredita aceptación del corte Git, **no producción web**. Después hacen
falta consumo/build web y verificación de dominio, alias y permalink con su orden.
Comparar release/hash y fechas de ingesta; `fetched_at` no fecha un boletín nuevo.
D5/F2 y el tablero sólo se actualizan con esa evidencia, conservando la fórmula.
