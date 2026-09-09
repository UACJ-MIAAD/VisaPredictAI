# Política de cohortes y baraja pre-registrada (E3)

> **Documento de congelación.** Se escribe y se commitea **antes de entrenar**. Todo lo que
> aparece aquí quedó fijado sin haber visto ningún resultado de esta campaña. Cambiarlo después
> de ver resultados no es ajustar un experimento: es inventarlo.

Autoridad legible por máquina: [`docs/cohort_deck.json`](cohort_deck.json), leído y validado por
`vp_model/deck.py`. El corredor **no acepta una receta que no esté en la baraja**, así que un
control no puede convertirse en primario sin editar el archivo pre-registrado — y eso deja rastro
en la historia del repositorio.

## Pregunta

¿Un modelo global entrenado **por cohorte de estabilidad** bate al piso naïve-1 **de su propia
cohorte**? E2 ya respondió que no, sin reentrenar, sobre lo que la campaña anterior había
puntuado (68 de 68 comparaciones concluyentes por debajo del piso, cero por encima). E3 entrena
de verdad, en CPU local, sobre el universo evaluable y con la partición congelada de E1.

## Universo

`evaluable`, tomado de `reports/eval/series_cohorts.json` (RULE v1.0.0, épica E1): **74 series**
con clave nominal `(país, categoría, tabla)`, repartidas en **39 estables** y **35 no estables**.
La cohorte `all` es la unión, y existe para poder decir si segmentar aporta algo frente a no
segmentar. Ninguna serie fuera del catálogo entra en un lane.

## Baraja primaria — como máximo tres recetas por cohorte

| receta | espacio | qué prueba |
|---|---|---|
| `deepar-legacy` | nivel | la configuración histórica que divergió (`~3.3`): pérdida Normal, escalado estándar, sin `valid_loss` ni recorte de gradiente. Es la hipótesis que E3 pone a prueba, no un adorno. |
| `deepar-robust` | primera diferencia | StudentT, escalado robusto, `valid_loss = MAE`, `lr = 1e-4`, parada temprana y recorte de gradiente. |
| `deepar-robust-levels` | nivel | idéntica a la anterior salvo el espacio de entrenamiento, para separar el efecto de la robustez del efecto de diferenciar. |

## Controles secundarios — predeclarados, nunca promovibles

`BiTCN`, `PatchTST`, `TiDE` y el GBM global (`lightgbm`) corren como **contexto**: no responden la
pregunta de E3 y no pueden ascender a primarios después de ver resultados. `AutoDeepAR` y
`AutoBiTCN` quedan **predeclarados pero no programados** en esta campaña: su búsqueda de
hiperparámetros no cabe en el presupuesto de CPU del lote. Declararlos aquí cierra la familia:
nada puede añadirse luego sin tocar este archivo.

## Congelado

`StudentT` · `valid_loss = MAE` · `lr = 1e-4` · escalado **robusto** · `--diff` donde la receta lo
declara · parada temprana con paciencia 5 · `gradient_clip_val = 1.0` · `max_steps = 1000` ·
horizonte 1 · `input_size` 36 (FAD) y 18 (DFF) · validación cruzada de 24 ventanas, paso 1, sin
reajuste, `val_size = 24` · columna de predicción **`DeepAR-median`** · `accelerator = "cpu"` ·
`torch.set_num_threads(1)` · semilla **1**.

`deepar-legacy` conserva a propósito sus valores históricos (Normal, escalado estándar, sin
`valid_loss`, sin recorte): congelar la receta significa preservarla como era, no modernizarla.

## Cómputo

**CPU local, y solo eso.** Sin EC2, sin A10G, sin CUDA y **sin MPS**, aunque esta máquina lo
tenga: el acelerador se fija a `cpu` explícitamente y una prueba lo comprueba. Sin despliegue,
sin promoción y sin subir pesos a Git ni al remoto DVC — los lanes escriben predicciones y
recibos, no modelos.

## Qué cuenta como resultado

Cada lane deja una **receipt** con receta, cohorte, tabla, hashes de entrada, entorno y lock,
semilla, dispositivo, tiempos, memoria residente, convergencia, avisos, presencia de NaN o Inf,
la excepción si la hubo y **los archivos realmente escritos**.

**Un fallo o una no convergencia es un resultado y se conserva.** No hay reintentos silenciosos
ni sustitución de recetas: si un lane muere, su receipt lo dice y la campaña sigue con el
siguiente. El resultado agregado, positivo o negativo, se publica tal cual salga.

## Entradas selladas

`docs/cohort_deck.json` registra el commit y el sha256 de: el catálogo de cohortes, el barrido de
E2, las dos serializaciones del panel, la configuración canónica, el módulo de estabilidad y los
locks. Si una entrada cambia, la baraja deja de describir la campaña que se corrió.
