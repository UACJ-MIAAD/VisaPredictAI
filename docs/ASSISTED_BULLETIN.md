# Boletín manual-automatizado: una entrada y publicación explícita

La persona descarga el PDF oficial y lo deja en `data/inbox/`. Poppler extrae
texto en local; el parser conserva fechas, C y U y exige cuatro matrices y 150
celdas. No se aplica OCR ni se inventan datos. Si hay varios PDF, indique FILE.

```sh
make bulletin MONTH=2026-12 FILE='/ruta/Visa Bulletin For December 2026.pdf'
```

Sin FILE se exige un único PDF en `data/inbox/`. Las dependencias son el perfil
Python del proyecto y Poppler (`pdftotext`). La caché histórica de
`data/snapshots/` debe estar completa: la orden falla antes de construir DuckDB
si faltan fuentes que el panel ya contiene. Restaurar esa caché no significa
volver a descargar ni modificar la fuente bloqueada.

La orden emite la ruta de un `receipt.json` en `data/pdf_intake/`. Archiva el PDF
y sus hashes; genera un candidato aislado con HTML, CSV raw, panel, DuckDB y
feed. Una entrada inválida conserva las salidas vivas y un recibo de fallo.
**No promueve el candidato sobre el checkout ni cambia S3, GitHub o Netlify.**
El recibo declara `scoring_monthly=not_executed`: preparar DuckDB todavía no
equivale a generar los pronósticos ni a publicar una release.

Después de revisar el recibo, y usando las fuentes exactas integradas y limpias
en `main`, la continuación explícita es:

```sh
make bulletin MONTH=2026-12 PUBLISH=1 \
  RECEIPT='data/pdf_intake/2026-12/.../receipt.json' WAIT_SECONDS=3600
```

El recibo debe haberse generado con el HEAD actual, limpio y coincidente con
`main` remoto. No se acepta FILE al publicar ni se vuelve a tomar una entrada
distinta de la revisada. Si cambió el código, prepare un recibo nuevo. Los nombres
FILE y RECEIPT viajan por el entorno, sin reinterpretarlos como código shell.

PUBLISH=1 produce efectos externos: comprueba el snapshot original o equivalente
preservado, carga a S3 mediante create-only, dispara el workflow mensual con
`skip_freeze=true` y un identificador ligado al recibo, y espera su corrida exacta
con cota. Ese workflow reconstruye y verifica los datos, produce pronósticos y
scoring, conserva el benchmark retrospectivo publicado, actualiza EDA/FE y sella
el manifiesto. Su entrega exige CI verde antes de Netlify. El heartbeat y las
alertas configurados pertenecen al workflow existente; esta orden no reabre la
campaña madre ni usa `sync_all --publish`.

El wrapper descarga la telemetría original y valida sus hashes y run_id incluso
ante un fallo de delivery. Solo declara éxito después de contrastar el manifiesto
y los artefactos consumidos en dominio y alias. Las esperas están acotadas; si se
agota el tiempo se conserva run_id. Reintente con el mismo recibo para inspeccionar
la corrida registrada; no lance un dispatch a ciegas. Un request ambiguo falla.

Un objeto S3 distinto se rechaza sin sobrescribir. Un objeto idéntico es un no-op.
Un mes ya sellado y servido con la misma release verifica consumidores y termina
sin reconstruir ni disparar otra corrida. Los reintentos nunca cuentan como meses
adicionales. Un fallo de entrega permanece registrado; un consumidor manual
posterior no lo convierte retroactivamente en entrega automática verde.

`publication/operation.json` conserva el vínculo entre recibo, request, HEAD,
run_id, telemetría y consumidores. La retención de GitHub no sustituye el archivo
durable local de la telemetría. Los respaldos y recibos anteriores se preservan.
