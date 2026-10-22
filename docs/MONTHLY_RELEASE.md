# Operación mensual después de la campaña

Un boletín nuevo actualiza el panel, el almacén, los pronósticos de la receta
desplegada, su registro prospectivo y los informes de datos. No autoriza otra
campaña, otro ajuste de hiperparámetros ni una promoción automática.

El veredicto retrospectivo `champion_challenger.json` y la receta
`champion_manifest.json` permanecen como referencia archivada del corte anterior.
El mensual contrasta sus blobs regulares y sus hashes con el manifiesto
commiteado, sin referencias de reemplazo de Git. Esto comprueba **integridad**,
no emite una acreditación nueva ni reemplaza la revisión humana de una campaña.
El productor retrospectivo exige una campaña running y por eso no se invoca
desde el cron ordinario. La decisión prospectiva sí se vuelve a calcular con
las observaciones nuevas; su aplicación continúa siendo humana.

El catálogo de cohortes, sus resultados E5 y las escalas de bandas también
pertenecen a sus cortes científicos. El mensual conserva sus hashes: no desplaza
retroactivamente la partición ni recalibra intervalos al ver datos nuevos. Las
pruebas de reproducción recuperan el panel y el ledger del commit en que se
derivó cada artefacto, sin referencias de reemplazo de Git. Se mantiene el piso
de cobertura de la validación original y se exige reproducir sus escalas y
resultados exactos. Esa prueba histórica no acredita cobertura en un mes posterior;
las mediciones nuevas corresponden al seguimiento prospectivo y se registran
con su población y período, sin mezclarlas con la validación original.

La rejilla de regresión de código y el pin histórico de avisos Zivot-Andrews usan
el panel publicado de septiembre, identificado por commit y SHA256 en
`tests/scientific_reference.py`. Construyen un almacén temporal mediante el mismo
productor de producción, sin reemplazar el almacén operativo. Sus tolerancias,
valores de referencia y lista estrecha de avisos no cambian. Un barrido adicional
del panel vivo rechaza mensajes nuevos. El deck conserva sus entradas originales;
el cotejo de su CSV y del objeto Parquet usa la época sellada, sin resellar el deck.
Las tablas descriptivas del manuscrito sí se recalculan con el panel mensual y
siguen comparándose fila por fila con el productor actual.

Las exportaciones `key_facts.tex` y `fe_facts.tex` pertenecen al mismo stage que
sus JSON. Se preparan por sus rutas exactas, sin admitir sufijos de respaldo ni
archivos del manuscrito. Tras reconstruir el panel se actualiza su estado
nominal, conservando el status, motivo, fecha y último éxito registrados de la
fuente. La expectativa del día 15 es una política de calendario, no prueba de
que el siguiente boletín exista.

El release gate sella los artefactos, actualiza las etiquetas numéricas del ER y
la arquitectura desde ese corte, comprueba de nuevo la referencia archivada y
ejecuta contratos y consistencia. Cualquier fallo detiene la cadena. Sólo con
el CI verde sobre el SHA final se dispara el hook del portal. El consumidor
verifica todos los hashes y conserva el corte anterior ante una incompatibilidad.

Las exportaciones y la prosa del repositorio documental y del portal también
deben reconciliarse con el corte. Preparar un panel local, archivar un snapshot
o repetir un build del portal no acredita un ciclo mensual completado.

La generación y las escrituras terminan en el job `update`, con un límite de
55 minutos, antes de que caduque su token de instalación. El job `delivery`
recibe el SHA y el identificador sellados mediante outputs; no tiene checkout,
token de la App ni permiso de escritura del repositorio. Espera con cota el CI
de ese SHA, dispara el hook sólo tras su éxito y comprueba el corte servido.
El correo de generación no equivale a esa verificación: el resultado final
queda en el resumen de `delivery`, cuyo fallo hace fallar el workflow.

## Almacén y transporte DVC del corte mensual

Después de reconstruir DuckDB/Parquet, la fase de datos registra juntos los stages
`scrape`, `panel`, `bulletins` y `database`. También sube únicamente el objeto
cacheado de `database` mediante `dvc push database`, antes del commit de datos.
El transporte S3 usa `dvc-s3==3.3.0`, fijado junto al DVC 3.67.1 que verifica CI,
bajo las constraints CPU vigentes para no desplazar fsspec ni las dependencias
compartidas antes de instalar el perfil de modelado.
Si el transporte falla, no se publica el lock nuevo. El CSV permanece abierto en
Git; DuckDB sigue siendo regenerable y no se añade al repositorio.
