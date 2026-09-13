"""Acredita la COBERTURA del árbol de finalistas y sólo entonces lo promueve. Fail-closed.

El agujero que cierra, medido en `rederiv_1022c9d_20260911T212150`: `save_finalists_deep.py` falló
y, aun así, `models/` quedó con **300 entradas locales nuevas y cero globales**, mientras los cinco
directorios globales del 26-ago seguían ahí. El exportador los leyó y los metió en el CSV. Nadie
mintió: simplemente **la frescura se infería por existencia**, y un directorio viejo existe igual
que uno nuevo.

Aquí la frescura se **acredita**:

1. los productores escriben en ``models/.staging/<campaign_id>/`` — el árbol servido no se toca;
2. se exige **cobertura exacta**: cada (tabla, modelo) declarado por los productores, cada serie
   del catálogo. Ni faltantes ni sobrantes;
3. sólo entonces se promueve, **apartando** el árbol anterior (no se borra: se retira con su fecha)
   y dejando un recibo con `campaign_id`, SHA del código, del panel y de cada artefacto.

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
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVED = ROOT / "models"
RETIRED = SERVED / ".retired"
RECEIPT = "coverage_receipt.json"
RECEIPT_SCHEMA = "finalists-coverage-receipt/1"


class CoverageError(RuntimeError):
    """El árbol de finalistas no cubre lo que los productores declaran producir."""


def expected_models() -> dict[str, tuple[str, ...]]:
    """Qué debe cubrir el árbol persistido. **Del registro canónico, no de cada productor.**

    ⚠️ La primera versión de este promotor derivaba las listas por AST de `save_finalists.py` y
    `save_finalists_deep.py`. Eso habría **trasladado** la divergencia que M74-E encontró —tres
    autoridades que ya discrepaban en `sarima`— en vez de cerrarla. La autoridad es
    `experiments/model_registry.py` y aquí sólo se consulta.

    ★ El promotor exige **sólo los persistibles**. `ets` y `theta` no lo son (AutoETS/AutoTheta no
    conservan estado reutilizable), y su ausencia del árbol **no** los vuelve opcionales: su
    cobertura la exige el EXPORTADOR sobre `REQUIRED_FORECAST_MODELS`.
    """
    # Vive JUNTO al registro (`experiments/`), así que al ejecutarse como guion su propio
    # directorio ya es `sys.path[0]` y el parche de ruta que había aquí sobra.
    from model_registry import GLOBAL_MODELS, LOCAL_MODELS, PERSISTED_MODELS

    persistidos = set(PERSISTED_MODELS)
    return {
        "local": tuple(m for m in LOCAL_MODELS if m in persistidos),
        "global": tuple(m for m in GLOBAL_MODELS if m in persistidos),
    }


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def audit_coverage(staging: Path, root: Path = ROOT) -> dict:
    """Compara lo producido contra lo declarado. Devuelve el censo; lanza si no cuadra."""
    manifiesto = staging / "manifest.jsonl"
    if not manifiesto.is_file():
        raise CoverageError(f"sin manifiesto en {manifiesto}: el productor no dejó constancia")
    entradas = [json.loads(ln) for ln in manifiesto.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not entradas:
        raise CoverageError("el manifiesto está vacío: ningún finalista se persistió")

    esperado = expected_models()
    visto: dict[str, set] = {"local": set(), "global": set()}
    for e in entradas:
        clase = "global" if "global" in str(e.get("type", "")) else "local"
        visto[clase].add((e.get("table"), e.get("model")))

    problemas: list[str] = []
    tablas = sorted({e.get("table") for e in entradas if e.get("table")})
    if len(tablas) != 2:
        problemas.append(f"tablas cubiertas {tablas}: se esperan las dos (FAD, DFF)")
    for clase, modelos in esperado.items():
        for tabla in tablas:
            faltan = [m for m in modelos if (tabla, m) not in visto[clase]]
            if faltan:
                problemas.append(f"{clase}/{tabla}: faltan {faltan}")
        sobran = sorted({m for t, m in visto[clase] if m not in modelos})
        if sobran:
            problemas.append(f"{clase}: sobran {sobran} (no declarados por el productor)")
    if problemas:
        raise CoverageError(
            "cobertura incompleta; NO se promueve nada (el árbol servido queda intacto):\n  - "
            + "\n  - ".join(problemas)
        )
    return {
        "entradas": len(entradas),
        "tablas": tablas,
        "modelos_por_clase": {k: sorted({m for _, m in v}) for k, v in visto.items()},
    }


def promote(staging: Path, *, campaign_id: str, root: Path = ROOT) -> Path:
    """Promueve el árbol acreditado. El anterior se RETIRA con fecha, nunca se borra."""
    censo = audit_coverage(staging, root)
    served = root / "models"
    sello = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for tabla in censo["tablas"]:
        origen, destino = staging / tabla, served / tabla
        if not origen.is_dir():
            raise CoverageError(f"el staging no tiene {origen}")
        if destino.exists():
            aparte = RETIRED / f"{sello}_{campaign_id}"
            aparte.mkdir(parents=True, exist_ok=True)
            os.replace(destino, aparte / tabla)  # atómico dentro del mismo sistema de archivos
        os.replace(origen, destino)

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
    args = ap.parse_args(argv)
    try:
        recibo = promote(Path(args.staging), campaign_id=args.campaign_id)
    except CoverageError as exc:
        print(f"✗ COBERTURA NO ACREDITADA · {exc}", file=sys.stderr)
        return 1
    print(f"✓ árbol de finalistas promovido y acreditado → {recibo.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
