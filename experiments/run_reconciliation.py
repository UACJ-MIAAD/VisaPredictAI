"""M74-F · G7 — CLI del recibo de reconciliación (`vp_model.reconciliation`).

Fino a propósito: cada subcomando es UNA transición del contrato o una lectura, y toda decisión vive en
`vp_model.reconciliation`. No re-deriva nada: entre ``assemble`` y ``close`` el operador corre los
constructores (`run_champion_challenger --reconciliation`, `build_key_facts`, `build_horizon_facts`,
`build_e5_facts`, `make_result_figures --crps-parent-root`, `make_hero_figures`), cada uno con su orden.

Ciclo (en un worktree DESECHABLE del commit que re-deriva, nunca en exec ni en una complementaria):

    assemble  re-acredita cada fuente (``--source``, una complementaria `accredited` por propósito) con su
              lector; extrae del tar sellado de la madre (``--evidence`` = su SHA256SUMS, copiado bajo
              reports/) sus salidas bajo reports/; copia los artefactos de las fuentes; sella el inventario
    verify    lector: re-verifica todo sin escribir (también re-puntúa E3)
    close     assembled → derived: sella el hash de las salidas re-derivadas (NO autoriza propagar)
    fail      aborto explícito → failed (terminal)

Uso:  ante/bin/python experiments/run_reconciliation.py <subcomando> --parent-root RAIZ_MADRE [...]
Códigos de salida: 0 bien · 2 la reconciliación no se acredita (los códigos van a stderr).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _contrato():
    """El contrato de ESTE árbol, no el del paquete instalado en el venv (que puede ser otro worktree)."""
    for ruta in (ROOT / "experiments", ROOT):
        if str(ruta) not in sys.path:
            sys.path.insert(0, str(ruta))
    from vp_model import reconciliation

    return reconciliation


def _lectores() -> dict:
    """Los lectores reales de E3: el acreditador de recibos y el puntuador, sobre el árbol de la fuente."""
    import score_e3_campaign as score

    def puntuar_e3(src: Path, parent_root: Path) -> dict:
        return score.puntuar(root=src, complemento=parent_root)

    return {"acreditar_lanes": score.acreditar_recibos, "puntuar_e3": puntuar_e3}


def cmd_assemble(args: argparse.Namespace) -> dict:
    rc = _contrato()
    doc = rc.ensamblar(args.root, parent_root=args.parent_root, evidence=args.evidence, sources=args.source,
                       reconciliation_id=args.id, **_lectores())  # fmt: skip
    return {"status": doc["status"], "reconciliation_id": doc["reconciliation_id"],
            "sources": {s["complement_id"]: sorted(s["artifacts"]) for s in doc["sources"]},
            "mother_layers": len(doc["mother_layers"]), "references": sorted(doc["references"]),
            "excluded": len(doc["excluded"]), "plan_sha256": doc["plan_sha256"]}  # fmt: skip


def cmd_verify(args: argparse.Namespace) -> dict:
    return _contrato().acreditar(args.root, parent_root=args.parent_root, **_lectores())


def cmd_close(args: argparse.Namespace) -> dict:
    doc = _contrato().cerrar(args.root, parent_root=args.parent_root, **_lectores())
    return {"status": doc["status"], "outputs": doc["outputs"], "note": "derived no autoriza propagar ni publicar"}


def cmd_fail(args: argparse.Namespace) -> dict:
    doc = _contrato().fallar(args.root, args.reason)
    return {"status": doc["status"], "failure": doc["failure"]}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="run_reconciliation", description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--root", type=Path, default=ROOT, help="raíz del worktree de reconciliación (por defecto, éste)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for nombre, fn in (("assemble", cmd_assemble), ("verify", cmd_verify), ("close", cmd_close), ("fail", cmd_fail)):
        sp = sub.add_parser(nombre, help=(fn.__doc__ or "").split("\n")[0] or None)
        sp.set_defaults(fn=fn)
        if nombre != "fail":
            sp.add_argument("--parent-root", type=Path, required=True, help="raíz de la campaña madre (sólo lectura)")
        if nombre == "assemble":
            sp.add_argument("--id", required=True, help="reconciliation_id: recon_<...>")
            sp.add_argument("--evidence", required=True, help="SHA256SUMS de la evidencia de la madre, bajo reports/")
            # G7 · B5a: sin `--source` es la reconciliación de una campaña SIN complementarias (sus propias
            # salidas): la única vía a publicar es una reconciliación + recibo /3, también para ella.
            sp.add_argument("--source", type=Path, action="append", default=[],
                            help="raíz de una complementaria accredited (una por propósito); ninguna = sin complementarias")  # fmt: skip
        if nombre == "fail":
            sp.add_argument("--reason", required=True)
    return ap


def main(argv: list[str] | None = None) -> int:
    rc = _contrato()
    args = build_parser().parse_args(argv)
    args.root = Path(args.root).resolve()
    try:
        out = args.fn(args)
    except rc.ReconciliacionError as exc:
        print(json.dumps({"ok": False, "codes": sorted(exc.codigos)}, ensure_ascii=False))
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, **out}, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
