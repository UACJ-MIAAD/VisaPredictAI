# ADR 0004 — Repositorio documental LaTeX independiente

- **Estado:** Aceptada; reemplaza la decisión documental de ADR 0001 §3.
- **Fecha:** 2026-09-09
- **Repositorio:** `UACJ-MIAAD/VisaPredictAI_LaTeX`
- **Verificación mecánica:** gate de compilación del repo LaTeX + guardián cross-repo
  de `VisaPredictAI`.

## Contexto

ADR 0001 mantuvo la tesis, el anteproyecto y el paper dentro del repositorio de
datos para conservar atomicidad. Esa decisión hizo que cada checkout de datos
arrastrara proyectos compilables, imágenes, clases y PDFs que sólo necesita el
flujo académico. También convirtió el repo de datos en el proyecto que Overleaf
debía sincronizar, mezclando dos ciclos de trabajo distintos.

La regla cero sigue siendo obligatoria: separar repositorios no autoriza que una
cifra derivada y su uso académico diverjan.

## Decisión

`VisaPredictAI_LaTeX` es la única autoridad compilable de:

- `reports/latex/` — anteproyecto y Proyecto I, incluidas sus figuras;
- `reports/paper_micai/` — manuscrito MICAI, clase, bibliografía y figuras;
- workflow y gate de logs de pdfLaTeX.

Overleaf se conecta exclusivamente a ese repositorio. `VisaPredictAI` deja de
versionar los proyectos compilables y sus assets.

Cinco fragmentos de texto siguen siendo, temporalmente, salidas del pipeline de
datos: `key_facts.tex`, `fe_facts.tex`, `horizon_champion.tex`,
`cohorts_facts.tex` y `cohortes.tex`. No forman por sí mismos un proyecto
compilable. El job `consistency` monta los repos web y LaTeX, compara estos cinco
bytes con sus copias consumidas y valida la prosa contra los JSON canónicos.

La excepción es deliberadamente transitoria: mover también la generación exige
un protocolo transaccional cross-repo equivalente al del release web. Hasta que
exista, borrar esas cinco salidas rompería DVC y ocultaría deriva.

## Consecuencias

- El checkout de datos pierde los binarios y fuentes académicas pesadas.
- El autor compila y sincroniza Overleaf desde un repo que contiene sólo el
  material documental.
- Un cambio de cifras requiere actualizar primero el repo LaTeX y después el de
  datos, o el guardián cross-repo falla cerrado.
- La compilación ya no es un job del repo de datos; su CI vive junto a los
  documentos que compila.
- La separación no cambia el release de producción, DVC remoto, modelos ni datos.

## Recuperación y procedencia

El repositorio documental nació mediante filtrado de historial desde
`VisaPredictAI@37afb31e41e33b422ddaae848ad8f9e5064bca03`. Su archivo
`MIGRATION_ORIGIN.json` registra el commit y los árboles exactos de las dos
superficies. El historial anterior también permanece en el repo de datos: esta
migración no reescribe ni borra commits publicados.

## Referencias

- [`../ARCHITECTURE.md`](../ARCHITECTURE.md) — límites vivos del workspace.
- [`../CONSISTENCY.md`](../CONSISTENCY.md) — regla cero y gate cross-repo.
- [`../STORAGE_POLICY.md`](../STORAGE_POLICY.md) — ubicación y retención.
- [`0001-project-boundaries.md`](0001-project-boundaries.md) — decisión anterior,
  reemplazada sólo en su §3.
