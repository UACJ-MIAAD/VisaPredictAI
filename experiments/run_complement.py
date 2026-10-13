"""M74-F · C3/C4 — CLI del ciclo de una transacción complementaria (`vp_model.complement`).

Fino a propósito: cada subcomando es UNA transición del contrato o una lectura, y toda decisión vive
en `vp_model.complement`. No entrena ni lanza lanes: C4 los corre `run_e3_campaign.py --complement`,
C3 `improve_conformal.py --complement`, C5 (CRPS campeón) `run_champion_crps.py --complement` y C6 (CRPS de
referencia) `run_crps_baseline.py --complement`, cada uno con su propia orden.

Ciclo (en un worktree DESECHABLE del commit de M74-F, nunca en exec):

    import  copia de la madre los artefactos que el propósito importa (antes de planificar)
    plan    sella id, madre, código, panel, DuckDB, entradas congeladas, lanes, semilla e importaciones,
            cada una contra su miembro del tar de evidencia de la madre (``--evidence`` = su SHA256SUMS,
            copiado bajo reports/ junto a campaign.json y salidas_campana_completa.tar.gz; obligatorio
            si el propósito importa algo — C5 no importa nada)
    start   planned → running
    close   running → computed   (el lector acredita lanes, importaciones y, en C3, la procedencia)
    accept  computed → accredited (NO autoriza propagar ni publicar)
    verify  lector: re-verifica todo sin escribir
    fail    aborto explícito → failed (terminal)

Uso:  ante/bin/python experiments/run_complement.py <subcomando> --parent-root RAIZ_MADRE [...]
Códigos de salida: 0 bien · 2 la complementaria no se acredita (los códigos van a stderr).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TABLAS = ("FAD", "DFF")


def _contrato():
    """El contrato de ESTE árbol, no el del paquete instalado en el venv (que puede ser otro worktree)."""
    for ruta in (ROOT / "experiments", ROOT):
        if str(ruta) not in sys.path:
            sys.path.insert(0, str(ruta))
    from vp_model import complement

    return complement


def importaciones(purpose: str) -> list[dict]:
    """Lo que cada propósito importa de la madre, con su protocolo y sus consumidores.

    La evidencia no es por importación: el plan acredita cada una contra su miembro del tar de la madre.
    """
    cx = _contrato()
    from vp_model.deck import RUTAS_ENTRADAS

    if purpose == "e3":
        # C4 · lo que la madre re-derivó y E3 consume: las cohortes (8c) y la referencia naïve-1 del puntuador.
        from score_e3_campaign import SCORECARDS

        decl = [
            {
                "artifact": RUTAS_ENTRADAS["cohort_scan.json"],
                "protocol": {"deck_input": "cohort_scan.json", "stage": "8c"},
                "consumers": ["lanes E3 (cohortes)"],
            }
        ]
        decl += [
            {
                "artifact": f"reports/eval/{n}",
                "protocol": {"scorecard": n, "reference": "naive1"},
                "consumers": ["score_e3_campaign.naive1_por_serie"],
            }
            for n in SCORECARDS
        ]
    elif purpose == "deep_pi_conformal":
        consumidores = {
            "baseline": ["improve_conformal.baseline_coverage", "make_result_figures.F4.base_nf"],
            "forecasts": ["improve_conformal._evaluate (gamma, ACI, split)"],
        }
        decl = []
        for t in TABLAS:
            for rol in cx.ROLES_C3:
                rel, proto = cx.artefacto_c3(rol, t)
                decl.append({"artifact": rel, "protocol": proto, "consumers": consumidores[rol]})
    elif purpose in {"champion_crps", "baseline_crps"}:
        decl = []  # C5/C6 calculan sobre el panel y la DuckDB sellados (C5: la receta es una entrada congelada)
    else:
        raise cx.ComplementoError([("proposito", f"{purpose!r} no es uno de {sorted(cx.PROPOSITOS)}")])
    return [{**d, "origin_path": d["artifact"]} for d in decl]


def entradas_congeladas(root: Path, purpose: str) -> dict[str, str]:
    """C4: las congeladas de la baraja (hash DECLARADO) y las derivadas que no se importan (hash vivo).
    C5: la receta campeona (`champion_manifest.json`), con su hash vivo al planificar."""
    if purpose == "champion_crps":
        cx = _contrato()
        manifiesto = root / cx.MANIFIESTO_CAMPEON_REL
        return {cx.MANIFIESTO_CAMPEON_REL: cx.ar.sha256_file(manifiesto) if manifiesto.is_file() else "ausente"}
    if purpose != "e3":
        return {}
    _contrato()
    from vp_model.deck import RUTAS_ENTRADAS, cargar_deck

    importadas = {d["artifact"] for d in importaciones(purpose)}
    out: dict[str, str] = {}
    for clave, c in cargar_deck(root / "docs" / "cohort_deck.json").contrastar_entradas(root).items():
        rel = RUTAS_ENTRADAS.get(clave, clave)
        if rel in importadas:
            continue
        sha = c["declared"] if c["kind"] == "congelada" else c["live"]
        out[rel] = sha if sha is not None else "ausente"
    return out


def _acreditar_lanes():
    from score_e3_campaign import acreditar_recibos

    return acreditar_recibos


def cmd_import(args: argparse.Namespace) -> dict:
    """Copia byte a byte de la madre. Sólo antes del plan: después, una importación no se toca."""
    cx = _contrato()
    root = args.root
    if (root / cx.RUTA_REL).exists():
        raise cx.ComplementoError([("ya-planificada", "con el plan sellado las importaciones no se tocan")])
    cx.leer_padre(args.parent_root)
    copiados = {}
    for d in importaciones(args.purpose):
        origen, destino = args.parent_root / d["origin_path"], root / d["artifact"]
        if not origen.is_file():
            raise cx.ComplementoError([("importacion-origen", f"{d['origin_path']}: no existe en la madre")])
        destino.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(origen, destino)
        if cx.ar.sha256_file(destino) != cx.ar.sha256_file(origen):
            raise cx.ComplementoError([("importacion-sustituida", f"{d['artifact']}: la copia no es el origen")])
        copiados[d["artifact"]] = cx.ar.sha256_file(destino)
    return {"imported": copiados}


def cmd_plan(args: argparse.Namespace) -> dict:
    cx = _contrato()
    from vp_model.deck import cargar_deck

    root = args.root
    deck = cargar_deck(root / "docs" / "cohort_deck.json")
    lanes = deck.lanes() if args.purpose == "e3" else []
    doc = cx.planificar(
        root,
        parent_root=args.parent_root,
        complement_id=args.id,
        purpose=args.purpose,
        lanes=lanes,
        seed=deck.semilla(),
        frozen_inputs=entradas_congeladas(root, args.purpose),
        imports=importaciones(args.purpose),
        evidence=args.evidence,
        git=cx.estado_git,
    )
    return {"status": doc["status"], "complement_id": doc["complement_id"], "lanes": len(doc["lanes"]),
            "imports": [i["artifact"] for i in doc["imports"]], "plan_sha256": doc["plan_sha256"]}  # fmt: skip


def cmd_start(args: argparse.Namespace) -> dict:
    cx = _contrato()
    doc = cx.iniciar(args.root, parent_root=args.parent_root, git=cx.estado_git)
    return {"status": doc["status"], "identity": cx.identidad(doc), "lanes_dir": doc["lanes_dir"]}


def cmd_close(args: argparse.Namespace) -> dict:
    cx = _contrato()
    doc = cx.cerrar_computo(args.root, parent_root=args.parent_root, acreditar_lanes=_acreditar_lanes(),
                            git=cx.estado_git)  # fmt: skip
    return {"status": doc["status"], "outcome": doc["outcome"]}


def cmd_accept(args: argparse.Namespace) -> dict:
    cx = _contrato()
    doc = cx.aceptar(args.root, parent_root=args.parent_root, acreditar_lanes=_acreditar_lanes(), git=cx.estado_git)
    return {"status": doc["status"], "note": "accredited no autoriza propagar ni publicar"}


def cmd_verify(args: argparse.Namespace) -> dict:
    cx = _contrato()
    return cx.acreditar(args.root, parent_root=args.parent_root, acreditar_lanes=_acreditar_lanes(), git=cx.estado_git)


def cmd_fail(args: argparse.Namespace) -> dict:
    cx = _contrato()
    doc = cx.fallar(args.root, args.reason)
    return {"status": doc["status"], "failure": doc["failure"]}


def build_parser() -> argparse.ArgumentParser:
    cx = _contrato()
    ap = argparse.ArgumentParser(prog="run_complement", description=__doc__.split("\n")[0])
    ap.add_argument("--root", type=Path, default=ROOT, help="raíz del worktree complementario (por defecto, éste)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for nombre, fn in (("import", cmd_import), ("plan", cmd_plan), ("start", cmd_start), ("close", cmd_close),
                       ("accept", cmd_accept), ("verify", cmd_verify), ("fail", cmd_fail)):  # fmt: skip
        sp = sub.add_parser(nombre, help=(fn.__doc__ or "").split("\n")[0] or None)
        sp.set_defaults(fn=fn)
        if nombre != "fail":
            sp.add_argument("--parent-root", type=Path, required=True, help="raíz de la campaña madre (sólo lectura)")
        if nombre in {"import", "plan"}:
            sp.add_argument("--purpose", required=True, choices=sorted(cx.PROPOSITOS))
        if nombre == "plan":
            sp.add_argument("--id", required=True, help="complement_id: compl_<...>")
            sp.add_argument("--evidence", default=None, help="SHA256SUMS de la evidencia de la madre, bajo reports/")
        if nombre == "fail":
            sp.add_argument("--reason", required=True)
    return ap


def main(argv: list[str] | None = None) -> int:
    cx = _contrato()
    args = build_parser().parse_args(argv)
    args.root = Path(args.root).resolve()
    try:
        out = args.fn(args)
    except cx.ComplementoError as exc:
        print(json.dumps({"ok": False, "codes": sorted(exc.codigos)}, ensure_ascii=False))
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, **out}, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
