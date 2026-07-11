"""Publicación por allowlist para el cron (C4, plan auditoría 2026-07-11).

Sustituye los `git add reports` del workflow mensual: cada fase del cron publica SOLO
los artefactos de su allowlist (derivada de lo que cada bloque produce de verdad —
verificado contra los commits históricos del cron) y **reporta a gritos** cualquier
archivo sucio fuera de ella en vez de barrerlo en silencio hacia el commit. Un archivo
extraño jamás bloquea la publicación del mes (exit 0), pero jamás viaja sin ser visto:
el resumen va al stdout del Action y, con ``--marker``, al correo SES del run.

    python tools/cron_publish.py --stage data|model|eda [--marker /tmp/x.txt]

Solo stagea (git add); el commit sigue en el workflow (identidad, mensaje, push).
Stdlib puro: corre en la fase de datos del cron, que no instala extras.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Allowlist por fase = prefijos de lo que ESA fase produce (verdad-de-tierra: commits
# reales del cron d1d10ed/4047c05 + outs de dvc.yaml). cleaning_ledger es un out del
# stage panel -> pertenece a la fase de DATOS (antes viajaba en el `git add reports`
# del bloque de modelado, un mes tarde respecto a su commit natural).
ALLOWLIST: dict[str, tuple[str, ...]] = {
    "data": (
        "data/raw/",
        "data/processed/",
        "dvc.lock",
        "reports/governance/cleaning_ledger.json",
    ),
    "model": (
        "dvc.lock",
        "reports/prospective/",
        "reports/governance/",
        "reports/latex/key_facts.tex",
        "reports/release/",
    ),
    "eda": (
        "dvc.lock",
        "reports/eda/",
        "reports/fe/",
        "reports/latex/fe_facts.tex",
        "reports/latex/Figures/",
        "reports/release/",
    ),
}
# Fuera de estos árboles el cron no publica nada; lo sucio ahí ni se stagea ni se reporta
# (p. ej. venvs locales) — el reporte de rechazados se limita al territorio publicable.
SCOPE = ("data/", "reports/", "dvc.lock")


def partition(dirty: list[str], stage: str) -> tuple[list[str], list[str]]:
    """(publicables, rechazados) para una fase — pura, testeable.

    Publicable = bajo un prefijo del allowlist de la fase. Rechazado = sucio dentro del
    territorio publicable (SCOPE) pero fuera del allowlist: visible, nunca barrido.
    """
    allow = ALLOWLIST[stage]
    publish, reject = [], []
    for p in dirty:
        if any(p == a or p.startswith(a) for a in allow):
            publish.append(p)
        elif any(p == s or p.startswith(s) for s in SCOPE):
            reject.append(p)
    return publish, reject


def _dirty_paths() -> list[str]:
    out = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", *SCOPE],
        text=True,
        cwd=ROOT,
    )
    paths = []
    for line in out.splitlines():
        # formato porcelain: XY <path> (renames: "old -> new" — publicamos el destino)
        p = line[3:].split(" -> ")[-1].strip().strip('"')
        if p:
            paths.append(p)
    return paths


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=sorted(ALLOWLIST))
    ap.add_argument("--marker", help="archivo de resumen para el correo SES del run")
    args = ap.parse_args()

    publish, reject = partition(_dirty_paths(), args.stage)
    if publish:
        subprocess.run(["git", "add", "--", *publish], cwd=ROOT, check=True)
    summary = f"publish[{args.stage}]: {len(publish)} archivo(s) publicados"
    if reject:
        summary += f" · {len(reject)} RECHAZADO(S) fuera del allowlist: {', '.join(sorted(reject)[:10])}"
    print(summary)
    for p in sorted(publish):
        print(f"  + {p}")
    if args.marker:
        Path(args.marker).write_text(summary + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
