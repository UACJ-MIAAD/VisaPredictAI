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
