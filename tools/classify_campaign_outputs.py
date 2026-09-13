"""Clasifica las salidas de una campaña. Las etiquetas son un CONJUNTO, no una casilla.

La corrida `rederiv_1022c9d_20260911T212150` dejó 77 entradas tocadas. Llamarlas «salidas
parciales» sería quedarse corto, y el matiz decide qué se puede tirar y qué hay que mirar dos veces:

* **completa** — la calculó esta campaña, y todas sus entradas también son de esta campaña.
* **incompleta** — la etapa que la produce falló: el artefacto no existe, está vacío o sólo tiene
  cabecera.
* **contaminada** — ★ la escribió esta campaña **sobre entradas de otra añada**. Es la peligrosa:
  tiene aspecto de resultado completo. En la corrida fallida, la etapa [4] combinó modelos nuevos
  con `holdout_forecasts_*.csv` de julio —que la etapa [3], fallida, debía haber refrescado— y
  publicó en el log `MASE 0.1413 / 0.1003` bajo el rótulo «combinadores sobre holdouts frescos».
  Nada en esa línea la distingue de una cifra buena.

★ **Dos correcciones de M74-E sobre mi propia primera versión, ambas por defectos reales:**

1. **Las etiquetas se acumulan.** La versión anterior decidía con `continue`: el primer veredicto
   ganaba y los demás no llegaban a evaluarse. Un artefacto **vacío Y con insumos rancios** salía
   sólo como «incompleta», y quien leyera esa lista creería que basta re-ejecutar su etapa. Son
   predicados independientes y se evalúan los tres.
2. **Los punteros DVC entraban como texto y no como artefacto.** `models.dvc` y `mlflow.db.dvc`
   son las DOS salidas más grandes de la campaña y eran invisibles: el inventario se tomaba de
   `git status reports/` y luego se filtraba a `.csv/.json/.jsonl`. El puntero no es el artefacto:
   lo que hay que fechar es su **carga útil**. Medido en la escena real, `models/` tiene **46
   archivos de junio y julio junto a 301 del día de la campaña**, y `sync_all LOCAL` re-hasheó el
   puntero sobre ese árbol mezclado — contaminación de libro que ninguna prueba miraba.

**Cómo se decide, sin adivinar:** un artefacto escrito dentro de la ventana de la campaña cuyos
insumos declarados NO se reescribieron dentro de esa ventana está contaminado; y un ÁRBOL cuyo
puntero se reescribió en la ventana pero que guarda dentro entradas anteriores está contaminado
por mezcla de añadas. El mapa consumidor → insumos es **explícito y versionado**; un consumidor
sin declarar es un fallo del gate, no un «se asume limpio»: preferimos que el mapa envejezca
ruidosamente.

⚠️ **Lo que este clasificador NO sabe ver**, dicho para que nadie lo suponga: un archivo truncado
a la mitad que conserve filas válidas pasa como completo. La incompletitud se detecta por ausencia,
tamaño cero o cabecera sin filas, que es lo que se puede afirmar mirando el artefacto.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TXN = ROOT / "reports" / "campaign" / "campaign.json"

#: Consumidor → insumos de los que depende su contenido. Declarado a mano **a propósito**: es la
#: relación que la campaña fallida demostró que existe y que nadie estaba comprobando.
CONSUMERS: dict[str, tuple[str, ...]] = {
    "reports/eval/ensemble_bestk_FAD.csv": ("reports/eval/holdout_forecasts_FAD.csv",),
    "reports/eval/ensemble_bestk_DFF.csv": ("reports/eval/holdout_forecasts_DFF.csv",),
    "reports/eval/conformal_coverage.csv": (
        "reports/eval/holdout_forecasts_FAD.csv",
        "reports/eval/holdout_forecasts_DFF.csv",
    ),
    "reports/eval/deep_pi_FAD.csv": ("reports/eval/holdout_forecasts_FAD.csv",),
    "reports/eval/deep_pi_DFF.csv": ("reports/eval/holdout_forecasts_DFF.csv",),
}
#: Artefactos que no dependen de otro artefacto de la misma campaña (sólo del panel y del código).
SELF_CONTAINED = (
    "reports/campaign/",
    "reports/eval/model_comparison_",  # copia directa de los pools de la etapa [2]
    "reports/eval/experiment_runs.jsonl",  # bitácora append-only del tracking
    "reports/eval/auto_arima_baseline.csv",  # se calcula del panel, no de otro artefacto
    "reports/eval/finalist_forecasts_",  # los produce la propia etapa [3] al guardar finalistas
    "reports/eval/transported_forecasts",  # §8.7: lo acredita su propio recibo cerrado
    "reports/governance/mlflow_sync_reconciliation.json",  # reconciliación del tracking, no una cifra
    # ★ `mlflow.db` lo destapó el propio gate en su primera corrida real: estaba sin declarar.
    # Es una bitácora ACUMULATIVA —guarda las corridas de todas las campañas— y no deriva ninguna
    # cifra de otro artefacto, así que ni se contamina ni se mezcla: «de esta campaña» es una
    # propiedad de sus FILAS, no del archivo. Tratarla como árbol de añadas la daría por
    # contaminada siempre, que es justo lo contrario de su función.
    "mlflow.db",
)
#: ★ Punteros DVC → la carga útil que fechan. El puntero es texto de unos cientos de bytes; el
#: artefacto es el árbol o la base que hay detrás, y es ESO lo que tiene añada.
DVC_PAYLOAD: dict[str, str] = {
    "models.dvc": "models",
    "mlflow.db.dvc": "mlflow.db",
}
#: Artefactos que son ÁRBOLES: su contaminación no viene de un insumo declarado sino de guardar
#: dentro entradas de otra añada. `models/` es el caso que la campaña fallida produjo.
TREE_ARTIFACTS = ("models",)

COMPLETA, INCOMPLETA, CONTAMINADA, FUERA = "completa", "incompleta", "contaminada", "fuera_de_campana"


def _campaign_window(txn: Path = TXN) -> tuple[str, datetime]:
    if not txn.is_file():
        raise SystemExit(f"✗ no hay transacción de campaña en {txn}: nada que clasificar")
    estado = json.loads(txn.read_text(encoding="utf-8"))
    inicio = estado.get("started_at")
    if not inicio:
        raise SystemExit("✗ la transacción no sella started_at")
    return str(estado.get("campaign_id", "")), datetime.fromisoformat(str(inicio).replace("Z", "+00:00"))


def _mtime(p: Path) -> datetime | None:
    return datetime.fromtimestamp(p.stat().st_mtime, UTC) if p.exists() else None


def _tree_mtimes(d: Path) -> list[datetime]:
    """Las añadas de TODOS los archivos del árbol. El mtime del directorio no sirve: cambia con
    cualquier alta o baja de una entrada directa y no dice nada de lo que hay tres niveles abajo."""
    return [datetime.fromtimestamp(f.stat().st_mtime, UTC) for f in d.rglob("*") if f.is_file()]


def payload_of(rel: str) -> str:
    """La ruta cuya añada importa. Para un puntero DVC, su carga útil; si no, ella misma."""
    return DVC_PAYLOAD.get(rel, rel)


def _esta_vacio(p: Path) -> str | None:
    """Motivo por el que el artefacto está estructuralmente vacío, o ``None`` si tiene contenido."""
    if p.is_dir():
        return None if any(p.rglob("*")) else "el árbol no tiene una sola entrada"
    if p.stat().st_size == 0:
        return "cero bytes"
    if p.suffix in (".csv", ".jsonl"):
        lineas = [ln for ln in p.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        minimo = 2 if p.suffix == ".csv" else 1  # un CSV necesita cabecera + al menos una fila
        if len(lineas) < minimo:
            return "sólo cabecera, sin filas" if p.suffix == ".csv" else "sin un solo registro"
    return None


def classify(rutas: list[str], *, root: Path = ROOT, txn: Path = TXN) -> dict[str, dict]:
    """Clasifica cada ruta con TODAS las etiquetas que le apliquen. Fail-closed ante lo no declarado.

    Devuelve ``{ruta: {"clases": [...], "razones": {clase: motivo}, "payload": ruta_real}}``.
    ``clases`` puede traer ``incompleta`` y ``contaminada`` a la vez: son predicados independientes
    y un artefacto a medias construido sobre insumos rancios es exactamente las dos cosas.
    """
    _, inicio = _campaign_window(txn)
    veredicto: dict[str, dict] = {}
    for rel in rutas:
        carga = payload_of(rel)
        ruta = root / carga
        clases: list[str] = []
        razones: dict[str, str] = {}

        if not ruta.exists():
            veredicto[rel] = {
                "clases": [INCOMPLETA],
                "razones": {INCOMPLETA: f"el artefacto no existe ({carga})"},
                "payload": carga,
            }
            continue

        es_arbol = ruta.is_dir()
        if es_arbol:
            fechas = _tree_mtimes(ruta)
        else:
            propia = _mtime(ruta)
            fechas = [propia] if propia is not None else []
        ultima = max(fechas) if fechas else None

        # ── (0) fuera de la ventana: no es salida de esta campaña, y ninguna otra etiqueta aplica
        if ultima is not None and ultima < inicio:
            veredicto[rel] = {
                "clases": [FUERA],
                "razones": {FUERA: f"nada dentro se escribió tras el arranque ({ultima.isoformat()})"},
                "payload": carga,
            }
            continue

        # ── (1) ¿incompleta? Independiente de con qué se construyó.
        vacio = _esta_vacio(ruta)
        if vacio:
            clases.append(INCOMPLETA)
            razones[INCOMPLETA] = f"la etapa que lo produce no lo dejó utilizable: {vacio}"

        # ── (2) ¿contaminada? Dos vías: insumos rancios, o un árbol de añadas mezcladas.
        if carga in TREE_ARTIFACTS:
            viejas = sorted({f.date().isoformat() for f in fechas if f < inicio})
            if viejas:
                n = sum(1 for f in fechas if f < inicio)
                clases.append(CONTAMINADA)
                razones[CONTAMINADA] = (
                    f"árbol de añadas MEZCLADAS: {n} de {len(fechas)} archivos son anteriores a la "
                    f"campaña ({', '.join(viejas)}) y el puntero se re-hasheó sobre el conjunto"
                )
        elif any(carga.startswith(pref) for pref in SELF_CONTAINED):
            pass  # no consume artefactos de la campaña; nada que contaminar
        elif carga in CONSUMERS:
            rancios = []
            for insumo in CONSUMERS[carga]:
                m = _mtime(root / insumo)
                if m is None or m < inicio:
                    rancios.append(f"{insumo} ({'ausente' if m is None else m.date().isoformat()})")
            if rancios:
                clases.append(CONTAMINADA)
                razones[CONTAMINADA] = "insumos de otra añada: " + ", ".join(rancios)
        else:
            raise SystemExit(
                f"✗ {rel} se escribió durante la campaña y NO está declarado en CONSUMERS, "
                "SELF_CONTAINED ni TREE_ARTIFACTS. Declara de qué depende: asumirlo limpio es "
                "como se colaron las cifras contaminadas."
            )

        if not clases:
            clases, razones = [COMPLETA], {COMPLETA: "escrita en la ventana y con insumos frescos"}
        veredicto[rel] = {"clases": clases, "razones": razones, "payload": carga}
    return veredicto


def _rutas_tocadas(raiz: Path) -> list[str]:
    """El inventario REAL de lo tocado, del repositorio entero y sin filtrar por extensión.

    ⚠️ La versión anterior hacía `git status --porcelain reports` y luego se quedaba con
    `.csv/.json/.jsonl`. Las dos salidas mayores de la campaña —el árbol de modelos y la base de
    MLflow, que viajan como punteros `.dvc` en la RAÍZ— caían por los dos filtros a la vez.
    """
    import subprocess

    fin = subprocess.run(["git", "status", "--porcelain", "-z"], cwd=raiz, capture_output=True, text=True, check=False)
    rutas: list[str] = []
    campos = [c for c in fin.stdout.split("\0") if c]
    i = 0
    while i < len(campos):
        entrada = campos[i]
        estado, ruta = entrada[:2], entrada[3:]
        i += 1
        if estado[0] in ("R", "C"):  # un renombrado trae su origen en el campo siguiente
            i += 1
        rutas.append(ruta)
    return rutas


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("rutas", nargs="*", help="rutas relativas a la raíz; por defecto, las tocadas según git")
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "--root",
        default=str(ROOT),
        help="checkout a inspeccionar; la escena de una campaña fallida suele vivir en OTRO worktree",
    )
    args = ap.parse_args(argv)
    raiz = Path(args.root).resolve()
    txn = raiz / "reports" / "campaign" / "campaign.json"

    rutas = args.rutas or _rutas_tocadas(raiz)
    veredicto = classify(rutas, root=raiz, txn=txn)

    if args.json:
        print(json.dumps(veredicto, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        for clase in (CONTAMINADA, INCOMPLETA, COMPLETA, FUERA):
            elegidos = {k: v for k, v in veredicto.items() if clase in v["clases"]}
            if elegidos:
                marca = "⚠️ " if clase == CONTAMINADA else "  "
                print(f"\n{marca}{clase.upper()} ({len(elegidos)})")
                for k, v in sorted(elegidos.items()):
                    otras = [c for c in v["clases"] if c != clase]
                    tambien = f"  [también: {', '.join(otras)}]" if otras else ""
                    print(f"    {k} — {v['razones'][clase]}{tambien}")

    sospechosas = {k: v for k, v in veredicto.items() if {CONTAMINADA, INCOMPLETA} & set(v["clases"])}
    if sospechosas:
        n_cont = sum(1 for v in sospechosas.values() if CONTAMINADA in v["clases"])
        # ⚠️ Con `--json`, stdout es SÓLO el documento. Mi primera versión pegaba este resumen
        # detrás del JSON y `... --json | jq` moría con «Extra data»: una salida legible por
        # máquina deja de serlo en cuanto se le añade una línea para humanos.
        salida = sys.stderr if args.json else sys.stdout
        print(f"\n⚠️ {len(sospechosas)} artefacto(s) no utilizable(s), {n_cont} de ellos CONTAMINADO(s).", file=salida)
        print("   Una contaminada no se reutiliza ni parcialmente: se recalcula o se tira.", file=salida)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
