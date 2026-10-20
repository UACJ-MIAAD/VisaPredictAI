"""Acredita la COBERTURA del árbol de finalistas y sólo entonces lo promueve. Fail-closed.

El agujero que cierra, medido en `rederiv_1022c9d_20260911T212150`: `save_finalists_deep.py` falló
y, aun así, `models/` quedó con **300 entradas locales nuevas y cero globales**, mientras los cinco
directorios globales del 26-ago seguían ahí. El exportador los leyó y los metió en el CSV. Nadie
mintió: simplemente **la frescura se infería por existencia**, y un directorio viejo existe igual
que uno nuevo.

Aquí la frescura se **acredita**:

1. los productores escriben en ``models/.staging/<campaign_id>/`` — el árbol servido no se toca;
2. se exige **cobertura exacta**: cada (tabla, modelo) declarado por el registro y, para los locales,
   **cada serie del catálogo piloto** con la misma autoridad que usan los productores. Ni faltantes
   ni sobrantes;
3. sólo entonces se promueve, **apartando** el árbol anterior (no se borra: se retira con su fecha)
   junto con su manifiesto, y dejando un recibo con `campaign_id`, SHA del código, del panel y de
   cada artefacto.

★ **R14 · lo que R1..R13 dejaron sin cablear.** `save_finalists.sh` exportaba `VP_MODELS_DIR` y
`VP_MODELS_MANIFEST` y **ningún productor los leía**: escribían en `models/` servido y este promotor
moría en `[2b/4]` buscando `staging/manifest.jsonl`, tras las horas de la etapa deep. Y aunque el
staging se hubiera llenado, `promote()` movía las tablas pero **jamás promovía el manifiesto**: el
gate de completitud habría leído el `models/manifest.jsonl` de la añada anterior con rutas ya
retiradas. Ahora el manifiesto se promueve con las rutas **reescritas al árbol servido**, y cada una
se verifica en disco antes de escribir. La cobertura por serie se derivaba «del catálogo» sólo en
la prosa: se comparaban pares (tabla, modelo).

★ **Vive en `experiments/`, no en `tools/`**, y el gate de LOC por rol de C9 lo destapó: no es un
guardián del repositorio sino **un paso de la campaña**, igual que sus hermanos de
`save_finalists.sh` (`save_finalists_deep`, `save_finalists`, `export_forecasts`,
`persist_transported_forecasts`). Haberlo puesto en `tools/` fue un error mío de clasificación.

⚠️ **Lo que NO hace:** no rellena huecos, no acepta «casi completo» y no tiene bandera para
saltarse la cobertura. Un modelo que no se pudo persistir es un fallo del productor, no una
excepción del promotor — declararlo opcional aquí sería cambiar el protocolo por la puerta de atrás.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVED = ROOT / "models"
RETIRED = SERVED / ".retired"
RECEIPT = "coverage_receipt.json"
RECEIPT_SCHEMA = "finalists-coverage-receipt/1"
MANIFEST_NAME = "manifest.jsonl"


class CoverageError(RuntimeError):
    """El árbol de finalistas no cubre lo que los productores declaran producir."""


def expected_models() -> dict[str, tuple[str, ...]]:
    """Qué debe cubrir el árbol persistido. **Del registro canónico, no de cada productor.**

    ⚠️ La primera versión de este promotor derivaba las listas por AST de `save_finalists.py` y
    `save_finalists_deep.py`. Eso habría **trasladado** la divergencia que M74-E encontró —tres
    autoridades que ya discrepaban en `sarima`— en vez de cerrarla. La autoridad es
    `vp_model/model_registry.py` y aquí sólo se consulta.

    ★ El promotor exige **sólo los persistibles**. `ets` y `theta` no lo son (AutoETS/AutoTheta no
    conservan estado reutilizable), y su ausencia del árbol **no** los vuelve opcionales: su
    cobertura la exige el EXPORTADOR sobre `REQUIRED_FORECAST_MODELS`.
    """
    from vp_model.model_registry import GLOBAL_MODELS, LOCAL_MODELS, PERSISTED_MODELS

    persistidos = set(PERSISTED_MODELS)
    return {
        "local": tuple(m for m in LOCAL_MODELS if m in persistidos),
        "global": tuple(m for m in GLOBAL_MODELS if m in persistidos),
    }


def expected_series() -> dict[str, tuple[tuple[str, str], ...]]:
    """★ R14 · las series ``(country, category)`` por tabla, con la MISMA autoridad que el productor
    local (`dataset.list_series(table, block="family", countries=PILOT_COUNTRIES)`)."""
    from vp_model import config, dataset

    almacen = ROOT / "data" / "processed" / "visapredict.duckdb"  # absoluto: el cwd no es la raíz con --root
    return {
        table: tuple(
            (r.country, r.category)
            for r in dataset.list_series(
                table=table, block="family", countries=config.PILOT_COUNTRIES, db_path=almacen
            ).itertuples()
        )
        for table in config.TABLES
    }


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _leer_manifiesto(manifiesto: Path) -> list[dict]:
    if not manifiesto.is_file():
        raise CoverageError(f"sin manifiesto en {manifiesto}: el productor no dejó constancia")
    entradas = [json.loads(ln) for ln in manifiesto.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not entradas:
        raise CoverageError("el manifiesto está vacío: ningún finalista se persistió")
    return entradas


def audit_coverage(
    staging: Path,
    root: Path = ROOT,
    *,
    series: dict[str, Iterable[tuple[str, str]]] | None = None,
) -> dict:
    """Compara lo producido contra lo declarado. Devuelve el censo; lanza si no cuadra.

    ``series`` entra por parámetro para que una prueba hermética fije el universo sin la base de
    datos; la campaña lo deriva con :func:`expected_series`.
    """
    entradas = _leer_manifiesto(staging / MANIFEST_NAME)
    esperado = expected_models()
    universo = {t: tuple(s) for t, s in (series if series is not None else expected_series()).items()}

    def _s(e: dict, k: str) -> str:
        return str(e.get(k, ""))

    local_visto = {
        (_s(e, "table"), _s(e, "model"), _s(e, "country"), _s(e, "category"))
        for e in entradas
        if "global" not in _s(e, "type")
    }
    global_visto = {(_s(e, "table"), _s(e, "model")) for e in entradas if "global" in _s(e, "type")}
    local_esperado = {(t, m, c, k) for t, ss in universo.items() for m in esperado["local"] for c, k in ss}
    global_esperado = {(t, m) for t in universo for m in esperado["global"]}

    problemas: list[str] = []
    tablas = sorted({_s(e, "table") for e in entradas if e.get("table")})
    if tablas != sorted(universo):
        problemas.append(f"tablas cubiertas {tablas}: se esperan {sorted(universo)}")
    for clase, visto, quiere in (("local", local_visto, local_esperado), ("global", global_visto, global_esperado)):
        faltan, sobran = sorted(map(str, quiere - visto)), sorted(map(str, visto - quiere))
        if faltan:
            problemas.append(f"{clase}: faltan {len(faltan)} de {len(quiere)}, p. ej. {faltan[:3]}")
        if sobran:
            problemas.append(
                f"{clase}: sobran {len(sobran)} no declarados por el registro/catálogo, p. ej. {sobran[:3]}"
            )
    if len(entradas) != len(local_visto) + len(global_visto):
        problemas.append(
            f"{len(entradas) - len(local_visto) - len(global_visto)} entrada(s) DUPLICADA(s) en el manifiesto"
        )
    if problemas:
        raise CoverageError(
            "cobertura incompleta; NO se promueve nada (el árbol servido queda intacto):\n  - "
            + "\n  - ".join(problemas)
        )
    return {
        "entradas": len(entradas),
        "tablas": tablas,
        "series_por_tabla": {t: len(ss) for t, ss in universo.items()},
        "modelos_por_clase": {"local": sorted(esperado["local"]), "global": sorted(esperado["global"])},
    }


def _reescribir_manifiesto(entradas: list[dict], staging: Path, root: Path) -> list[dict]:
    """★ R14 · las rutas del staging pasan a apuntar al árbol servido, y cada una se comprueba en disco."""
    prefijo = staging.resolve().relative_to(root.resolve()).as_posix() + "/"
    salida: list[dict] = []
    for e in entradas:
        ruta = str(e.get("path", ""))
        if not ruta.startswith(prefijo):
            raise CoverageError(f"la entrada {e.get('table')}/{e.get('model')} apunta fuera del staging: {ruta!r}")
        nueva = "models/" + ruta[len(prefijo) :]
        if not (root / nueva).exists():
            raise CoverageError(f"tras promover no existe {nueva}: el manifiesto no describe el árbol servido")
        salida.append({**e, "path": nueva})
    return salida


def promote(
    staging: Path,
    *,
    campaign_id: str,
    root: Path = ROOT,
    series: dict[str, Iterable[tuple[str, str]]] | None = None,
) -> Path:
    """Promueve el árbol acreditado. El anterior se RETIRA con fecha, nunca se borra."""
    staging = staging if staging.is_absolute() else root / staging
    censo = audit_coverage(staging, root, series=series)
    entradas = _leer_manifiesto(staging / MANIFEST_NAME)
    served = root / "models"
    retired = served / ".retired"
    sello = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    aparte = retired / f"{sello}_{campaign_id}"
    for tabla in censo["tablas"]:
        origen, destino = staging / tabla, served / tabla
        if not origen.is_dir():
            raise CoverageError(f"el staging no tiene {origen}")
        if destino.exists():
            aparte.mkdir(parents=True, exist_ok=True)
            os.replace(destino, aparte / tabla)  # atómico dentro del mismo sistema de archivos
        os.replace(origen, destino)
    # el manifiesto anterior se retira con su árbol y el nuevo describe SÓLO el árbol servido
    manifiesto = served / MANIFEST_NAME
    if manifiesto.exists():
        aparte.mkdir(parents=True, exist_ok=True)
        os.replace(manifiesto, aparte / MANIFEST_NAME)
    promovidas = _reescribir_manifiesto(entradas, staging, root)
    tmp_m = manifiesto.with_suffix(".jsonl.tmp")
    tmp_m.write_text("".join(json.dumps(e) + "\n" for e in promovidas), encoding="utf-8")
    os.replace(tmp_m, manifiesto)

    recibo = served / RECEIPT
    artefactos = sorted(p for p in served.rglob("*") if p.is_file() and ".retired" not in p.parts)
    recibo_datos = {
        "schema": RECEIPT_SCHEMA,
        "campaign_id": campaign_id,
        "promoted_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "coverage": censo,
        "n_files": len(artefactos),
        "tree_sha256": hashlib.sha256(
            "".join(f"{p.relative_to(served)}:{_sha256(p)}" for p in artefactos if p.name != RECEIPT).encode()
        ).hexdigest(),
    }
    tmp = recibo.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(recibo_datos, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, recibo)
    shutil.rmtree(staging, ignore_errors=True)
    if staging.parent.name == ".staging" and staging.parent.is_dir() and not any(staging.parent.iterdir()):
        staging.parent.rmdir()  # el contenedor de staging vacío no es un artefacto: no queda en el árbol servido
    return recibo


def fresh_receipt(campaign_id: str, root: Path = ROOT) -> dict:
    """Para CONSUMIDORES: el recibo del árbol servido, o un fallo. Nunca inferir por mtime."""
    recibo = root / "models" / RECEIPT
    if not recibo.is_file():
        raise CoverageError(
            f"el árbol de modelos no lleva recibo ({recibo}): no puede acreditarse que sea de esta "
            "campaña. Existir no es estar fresco."
        )
    datos = json.loads(recibo.read_text(encoding="utf-8"))
    if datos.get("schema") != RECEIPT_SCHEMA:
        raise CoverageError(f"recibo con esquema {datos.get('schema')!r}")
    if datos.get("campaign_id") != campaign_id:
        raise CoverageError(
            f"el árbol de modelos es de la campaña {datos.get('campaign_id')!r} y la activa es "
            f"{campaign_id!r}: son añadas distintas"
        )
    return datos


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--staging", required=True)
    ap.add_argument("--campaign-id", required=True)
    ap.add_argument("--root", default=str(ROOT), help="raíz del árbol servido (las pruebas apuntan a un temporal)")
    args = ap.parse_args(argv)
    try:
        recibo = promote(Path(args.staging), campaign_id=args.campaign_id, root=Path(args.root).resolve())
    except CoverageError as exc:
        print(f"✗ COBERTURA NO ACREDITADA · {exc}", file=sys.stderr)
        return 1
    print(f"✓ árbol de finalistas promovido y acreditado → {recibo}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
