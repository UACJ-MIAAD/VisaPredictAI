"""¿El linaje de procedencia cubre EXACTAMENTE los snapshots elegibles? Fail-closed.

★ B4 de la auditoría `8656cf44…`. El preflight sella `data/snapshots`, **pero un directorio vacío
también se sella**: un hash de un conjunto vacío es un hash perfectamente válido. Y la etapa 0 del
runbook acepta `build degraded` y **sale 0**, así que una campaña podía arrancar con
`source_artifact` vacía y terminar en verde sin una sola fila de linaje. Medido en el worktree de
ejecución antes de materializar los snapshots: `source_artifact: 0 filas`, `build degraded`, rc 0.

Lo que se exige aquí, todo derivado y nada tecleado:

* **snapshots no vacíos** — el conjunto elegible se deriva con `extract_datetime_from_link`, la
  MISMA autoridad que usa `pipeline/db_loaders.py` para construir la tabla; derivarlo aparte sería
  otra lista que diverge;
* **cobertura EXACTA** — conjunto de nombres elegibles == conjunto de `filename` en la tabla, sin
  ausentes ni sobrantes;
* **hashes idénticos** a los bytes vivos, uno a uno;
* **sin duplicados** de `filename` ni de `sha256` con vintages distintos;
* **build no degradado** — `etl_run.build_status` debe ser `ok`.

⚠️ Comprueba el ALMACÉN contra el DISCO. No dice si los snapshots son los correctos: eso lo sella
el preflight. Dice que lo que el almacén afirma sobre ellos es cierto.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class LineageError(RuntimeError):
    """El linaje no cubre lo que el disco contiene, o el build salió degradado."""


def eligible_snapshots(snapshots: Path) -> dict[str, str]:
    """`{filename: sha256}` de los snapshots CON mes mapeable. Misma autoridad que el cargador."""
    from vp_data.visa_common import extract_datetime_from_link

    salida: dict[str, str] = {}
    for fp in sorted(Path(snapshots).glob("*.html")):
        if extract_datetime_from_link(fp.name) is None:
            continue  # páginas de anuncio: fuera del alcance de procedencia, como en db_loaders
        salida[fp.name] = hashlib.sha256(fp.read_bytes()).hexdigest()
    return salida


def verify(root: Path = ROOT) -> dict[str, int]:
    """Levanta `LineageError` con TODOS los problemas, no sólo el primero."""
    import duckdb

    snapshots = Path(root) / "data" / "snapshots"
    db = Path(root) / "data" / "processed" / "visapredict.duckdb"
    if not snapshots.is_dir():
        raise LineageError(f"no existe {snapshots}: sin snapshots no hay procedencia que acreditar")
    if not db.is_file():
        raise LineageError(f"no existe {db}: corre `python -m pipeline.build_database` antes")

    vivos = eligible_snapshots(snapshots)
    if not vivos:
        raise LineageError(
            f"{snapshots} no tiene un solo snapshot con mes mapeable. ⚠️ Un directorio vacío se "
            "sella igual de bien que uno lleno: por eso esta comprobación existe aparte del sello."
        )

    con = duckdb.connect(str(db), read_only=True)
    try:
        filas = con.execute("SELECT filename, sha256, vintage FROM source_artifact").fetchall()
        estado = con.execute("SELECT build_status FROM etl_run ORDER BY run_id DESC LIMIT 1").fetchone()
    finally:
        con.close()

    problemas: list[str] = []
    if not estado or estado[0] != "ok":
        problemas.append(f"el último build es {estado[0] if estado else 'desconocido'!r} y no `ok`")

    tabla = {f: s for f, s, _v in filas}
    if len(tabla) != len(filas):
        problemas.append(f"`filename` duplicado en source_artifact: {len(filas)} filas, {len(tabla)} nombres")
    faltan = sorted(set(vivos) - set(tabla))
    sobran = sorted(set(tabla) - set(vivos))
    if faltan:
        problemas.append(f"{len(faltan)} snapshot(s) elegible(s) SIN fila de linaje, p. ej. {faltan[:3]}")
    if sobran:
        problemas.append(f"{len(sobran)} fila(s) de linaje sin snapshot en disco, p. ej. {sobran[:3]}")
    distintos = sorted(f for f in set(vivos) & set(tabla) if vivos[f] != tabla[f])
    if distintos:
        problemas.append(f"{len(distintos)} hash(es) distintos de los bytes vivos, p. ej. {distintos[:3]}")
    por_sha: dict[str, set[str]] = {}
    for _f, s, v in filas:
        por_sha.setdefault(s, set()).add(str(v))
    ambiguos = sorted(s for s, vs in por_sha.items() if len(vs) > 1)
    if ambiguos:
        problemas.append(f"{len(ambiguos)} sha256 con vintages distintos: el mismo byte en dos meses")

    if problemas:
        raise LineageError("linaje de procedencia roto: " + " · ".join(problemas))
    return {"n_snapshots": len(vivos), "n_linaje": len(tabla)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=str(ROOT))
    args = ap.parse_args(argv)
    try:
        censo = verify(Path(args.root).resolve())
    except LineageError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    print(f"✓ linaje OK — {censo['n_snapshots']} snapshots elegibles, {censo['n_linaje']} filas, hashes idénticos")
    return 0


if __name__ == "__main__":
    sys.exit(main())
