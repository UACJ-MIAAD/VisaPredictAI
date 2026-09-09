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


---

# E4 · Router por estabilidad (pre-registro)

> Escrito y commiteado **antes** de calcular ningún resultado multi-horizonte del router.
> Autoridad legible por máquina: [`docs/challenger_deck.json`](challenger_deck.json), leído
> **únicamente** por `champion.load_deck()`, que falla cerrado si el archivo contradice lo que el
> código tiene congelado.

## Corrección a lo publicado en E3

La redacción de E3 decía que StudentT, la mediana, el escalado robusto y el recorte de gradiente
«eran la causa» de la divergencia histórica. **El diseño no permite esa atribución**: `legacy` y
`robust` difieren en **dos** cosas a la vez (el paquete de robustez y el espacio de
entrenamiento), y los elementos del paquete cambian **juntos**. Descompuesto en comparaciones de
un solo factor: el **espacio diferenciado** mejora en **6/6** celdas, y el **paquete robusto**, a
espacio fijo, mejora solo en **3/6** —las de DFF— y **empeora en las tres de FAD**. Ningún
elemento individual queda identificado, y separarlos pediría un diseño factorial que E3 no corrió.

## Pregunta

¿Enrutar por cohorte de estabilidad bate al naïve-1 **de cada cohorte** a 3, 6 y 12 meses?

## Lo que ya estaba congelado desde julio

| | valor | dónde |
|---|---|---|
| efecto material mínimo | **0.005** de MASE medio | `champion.MATERIAL_MARGIN` |
| alfa de Holm | **0.05** | `champion.HOLM_ALPHA` |
| contraste primario | Wilcoxon pareado **bilateral** | `champion._compare` |
| horizontes | {3, 6, 12} ⊂ `HORIZONS` | `config.HORIZONS` |
| candidatos del router | `naive1`, `drift`, `naive`, `theta`, `ets` | `config.HORIZON_CANDIDATES` |

Los candidatos son **clásicos**: la lista no incluye ningún modelo profundo, así que el router
**no puede** escoger la receta ganadora de E3 ni `control-bitcn`. No se incorporan `AutoDeepAR`
ni `AutoBiTCN`.

## Regla de selección — leakage-free por construcción

Por (tabla, cohorte, horizonte) se elige el candidato con **menor MASE medio sobre los objetivos
anteriores al hold-out**. La evaluación usa **solo objetivos del hold-out**. Selección y
evaluación son **disjuntas en el tiempo**: el router nunca ve el dato con el que se le juzga.

## Regla de victoria

El router **gana** solo si, **en los tres horizontes**: el efecto medio a su favor es **≥ 0.005**,
el Wilcoxon **bilateral** pareado sobrevive a **Holm** dentro de su familia —los horizontes
{3,6,12} de una misma tabla × cohorte—, y **no pierde de forma material en ninguno**
(`allowed_material_loss = 0`). Cualquier prueba unilateral se reporta **como sensibilidad**,
nunca como gate.

## Pool

Las **74** series de E1, persistidas en `reports/eval/e4_router_pool.json` y comparadas por
**igualdad de conjuntos** contra `dataset.evaluable_series()`.

## Frontera

**El resultado negativo también cierra E4.** Pase o no pase el gate retrospectivo, el router
**no se promueve ni se despliega**: eso exige sombra prospectiva y una autorización aparte.
