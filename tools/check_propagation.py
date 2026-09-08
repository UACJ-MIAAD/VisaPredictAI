"""F8 · Precondiciones de la propagación DATA -> WEB -> PROD. SOLO LECTURA.

El orden no es una costumbre: el job `consistency` del repo de datos valida contra la rama por
defecto del repo WEB, así que una regla nueva que prohíba algo que el web todavía dice deja el PR
de datos rojo por construcción; y el sitio no queda `fresh` hasta que el manifiesto vive en
`main` de datos. Ese orden se aprendió a golpes (M33) y aquí se comprueba antes de mover nada.

Este comando **no modifica, no regenera, no descarga, no pushea y no despliega**: solo lee el
árbol, el manifiesto, los artefactos y el pin del web. Fail-closed: cualquier insumo ausente,
ilegible o divergente es un fallo con su motivo, nunca un aviso.

    make propagate-check          # gate de precondiciones
    python tools/check_propagation.py --json

El repo web se localiza por `VP_WEB_DIR` (igual que el guardián) o por la convención de directorio
hermano; NUNCA por una ruta personal escrita aquí.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = Path(os.environ.get("VP_WEB_DIR", ROOT.parent / "VisaPredictAI_web"))
MANIFEST_REL = "reports/release/release_manifest.json"
PIN_REL = "lib/content/data-pins.generated.mjs"
PLAN_REL = "lib/plan-data.ts"


def _git(repo: Path, *args: str) -> str | None:
    """Lectura de git, sin efectos. `None` si el repositorio no responde."""
    try:
        out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, timeout=30)
    except subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError:
        return None
    return out.stdout.strip()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path, problems: list[str], label: str) -> dict[str, Any] | None:
    if not path.exists():
        problems.append(f"{label}: ausente ({path})")
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        problems.append(f"{label}: ilegible ({exc})")
        return None
    if not isinstance(data, dict):
        problems.append(f"{label}: la raíz no es un objeto")
        return None
    return data


def check(root: Path = ROOT, web: Path = WEB_DIR) -> tuple[dict[str, Any], list[str]]:
    problems: list[str] = []
    estado: dict[str, Any] = {"data_repo": str(root), "web_repo": str(web)}

    # 1) Limpieza: propagar desde un árbol sucio publica algo que nadie revisó.
    for nombre, repo in (("datos", root), ("web", web)):
        porcelain = _git(repo, "status", "--porcelain")
        if porcelain is None:
            problems.append(f"repo {nombre}: no responde a git ({repo}) — sin él no se puede propagar")
            estado[f"{nombre}_clean"] = None
            continue
        estado[f"{nombre}_clean"] = porcelain == ""
        if porcelain:
            problems.append(f"repo {nombre}: árbol sucio ({len(porcelain.splitlines())} entradas)")

    # 2) Autoridades y manifiesto.
    manifest = _read_json(root / MANIFEST_REL, problems, "manifiesto de release")
    if manifest is not None:
        estado["release_id"] = manifest.get("release_id")
        estado["manifest_git_sha"] = manifest.get("git_sha")
        criticos = [a for a in manifest.get("artifacts", []) if a.get("criticality") == "critical"]
        estado["n_critical"] = len(criticos)
        if not criticos:
            problems.append("manifiesto: sin artefactos `critical` — no hay autoridad que verificar")
        for entry in criticos:
            ruta = root / entry["path"]
            if not ruta.exists():
                problems.append(f"artefacto crítico ausente: {entry['path']}")
                continue
            got = _sha256(ruta)
            if got != entry.get("sha256"):
                problems.append(
                    f"artefacto crítico manipulado: {entry['path']} sha {got[:12]} != {str(entry.get('sha256'))[:12]}"
                )

    # 3) Pin del web contra el manifiesto.
    pin_file = web / PIN_REL
    if not pin_file.exists():
        problems.append(f"pin del web ausente ({PIN_REL}) — el sitio no declara qué corte sirve")
    else:
        texto = pin_file.read_text(errors="ignore")
        try:
            crudo = texto[texto.index("{") : texto.rindex("}") + 1]
            pin = json.loads(crudo)
        except (ValueError, json.JSONDecodeError) as exc:
            problems.append(f"pin del web ilegible ({exc})")
            pin = None
        if pin is not None:
            estado["web_release_id"] = pin.get("releaseId")
            estado["web_release_status"] = pin.get("releaseStatus")
            if manifest is not None and pin.get("releaseId") != manifest.get("release_id"):
                problems.append(
                    f"pin divergente: el web sirve {pin.get('releaseId')!r} y el manifiesto declara "
                    f"{manifest.get('release_id')!r} — publica datos ANTES que web"
                )

    # 4) Orden DATA -> WEB: el commit de datos que el web declara debe existir en el historial
    #    de datos. Si el web nombra algo que datos no tiene, se propagó al revés.
    plan = web / PLAN_REL
    if not plan.exists():
        problems.append(f"plan del web ausente ({PLAN_REL})")
    else:
        texto = plan.read_text(errors="ignore")
        marca = 'dataMain: "'
        if marca not in texto:
            problems.append("plan del web: sin `dataMain` — no declara el corte de datos que describe")
        else:
            inicio = texto.index(marca) + len(marca)
            data_main = texto[inicio : texto.index('"', inicio)]
            estado["web_data_main"] = data_main
            if _git(root, "cat-file", "-e", f"{data_main}^{{commit}}") is None:
                problems.append(
                    f"orden invertido: el web declara el corte de datos {data_main[:12]}, que este repo no tiene"
                )
            elif _git(root, "merge-base", "--is-ancestor", data_main, "HEAD") is None:
                problems.append(
                    f"orden invertido: {data_main[:12]} no es ancestro de HEAD en datos — el web va por delante"
                )

    return estado, problems


def main() -> int:
    estado, problems = check()
    if "--json" in sys.argv:
        print(json.dumps({"state": estado, "problems": problems}, indent=2, ensure_ascii=False))
    if problems:
        print(f"✗ PROPAGACIÓN BLOQUEADA ({len(problems)}):")
        for p in problems:
            print(f"  - {p}")
        print("  El orden es DATA → WEB → PROD; ver docs/RUNBOOK_PROPAGATION.md")
        return 1
    if "--json" not in sys.argv:
        print(
            f"✓ Precondiciones OK — corte {estado.get('release_id')} · {estado.get('n_critical')} artefactos "
            f"críticos verificados · web sirve {estado.get('web_release_id')} ({estado.get('web_release_status')})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
