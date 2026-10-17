"""M74-F · G7 · B5a — la autorización de publicar ligada al RESULTADO EXACTO que se revisó.

Una campaña con complementarias no publica sus propias salidas: publica lo que la reconciliación re-derivó, llevado a
una rama de integración (el *candidato*) junto con el manifiesto del corte. El recibo de validación
`campaign-validation-receipt/3` liga la decisión humana a todo eso, y este lector lo re-acredita EN VIVO cada vez que
alguien lo invoca (`validate`, `guard`, `publish`):

* **madre**: la evidencia de su cómputo (`campaign_evidence`) es la que el recibo nombra;
* **reconciliación**: el `reconciliation.json` tiene el hash del recibo, está en `derived`, es de ESTA madre y se
  re-acredita con su propio lector (`run_reconciliation verify`, con los lectores reales de E3). Ese archivo sella
  fuentes, capas, recibos sustituidos y salidas: fijar su hash los fija a todos;
* **candidato**: HEAD y árbol son los del recibo, el árbol de trabajo está limpio DE VERDAD (no el `git_dirty`
  sellado de la madre, que describe el árbol que calculó), el manifiesto de release es el revisado, y cada reporte
  versionado es byte a byte el de la reconciliación —salvo los dos artefactos del corte—, sin sobrar ninguno.

`publish` añade la última pieza: el commit que se publica tiene EXACTAMENTE el árbol revisado (`arbol_de`).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from vp_model import campaign_evidence as ce
from vp_model import reconciliation as rc

RECEIPT_SCHEMA = "campaign-validation-receipt/3"
#: Lo que el recibo /3 añade al /2: el resultado exacto que se revisó.
VINCULO = ("mother_computed_sha256", "reconciliation_root", "reconciliation_id", "reconciliation_sha256",
           "candidate_root", "candidate_git_sha", "candidate_tree_sha", "release_manifest_sha256", "release_id")  # fmt: skip
MANIFIESTO_RELEASE = "reports/release/release_manifest.json"
#: Lo que emite el CORTE sobre lo reconciliado (`make release-manifest`): no lo aporta la reconciliación.
CORTE = frozenset({MANIFIESTO_RELEASE, "reports/governance/MODEL_CARD.md"})
#: Contrato de EXPORTACIÓN (auditoría 17-oct): cada salida re-derivada que NO viaja a la rama de datos, con su destino
#: concreto. Todo lo demás que aporta la reconciliación es obligatorio en el candidato de datos. Las reglas de ignorado
#: de git (`.gitignore`, `.git/info/exclude`) no deciden qué evidencia es obligatoria: las controla quien publica.
EXPORTACION: dict[str, tuple[str, str]] = {
    **{f"{rc.FIGURAS}{n}.pdf": ("documental", f"reports/latex/Figures/{n}.pdf") for n in rc.FIGURAS_PDF},
    "reports/figures/results_holdout_mexico_F3_FAD.png": (
        "documental",
        "reports/latex/Figures/results_holdout_mexico_F3_FAD.png",
    ),
    "reports/paper_micai/Figures/fig_cd_diagram.pdf": ("documental", "reports/paper_micai/Figures/fig_cd_diagram.pdf"),
    **{
        f"reports/figures/cohorts/{d}e5_router_effect.png": (
            "local",
            "vista previa de make_cohort_figure; sin consumidor",
        )
        for d in ("", "dark/", "en/", "en/dark/")
    },
}
#: Lo que la reconciliación guarda para sí: jamás viaja al candidato.
_INTERNOS = ("reports/campaign/reconciliation/", "reports/campaign/complement_evidence/", str(rc.RUTA_REL))


def _sha(ruta: Path) -> str:
    return hashlib.sha256(ruta.read_bytes()).hexdigest()


def _git(raiz: Path, *args: str) -> str | None:
    fin = subprocess.run(["git", "-C", str(raiz), *args], capture_output=True, text=True, check=False)
    return fin.stdout if fin.returncode == 0 else None


def arbol_de(candidate_root: str | Path, commit: str) -> str | None:
    """El árbol git de ``commit`` en el repo candidato (``None`` si no existe ahí)."""
    salida = _git(Path(candidate_root), "rev-parse", "--verify", "-q", f"{commit}^{{tree}}")
    return salida.strip() if salida else None


def verificar_reconciliacion(raiz: Path, madre: Path, *, repo_root: Path) -> tuple[bool, str]:
    """El LECTOR real de la reconciliación (CLI `verify`, lectores de E3 incluidos), en un proceso aparte."""
    fin = subprocess.run(
        [sys.executable, str(repo_root / "experiments" / "run_reconciliation.py"), "--root", str(raiz), "verify",
         "--parent-root", str(madre)],
        cwd=str(raiz), capture_output=True, text=True, check=False, timeout=3600,
        env={**os.environ, "PYTHONPATH": str(repo_root)},
    )  # fmt: skip
    return fin.returncode == 0, (fin.stdout or "") + (fin.stderr or "")


def problema(acta: Mapping[str, str], txn_path: Path, estado: Mapping[str, Any], *, repo_root: Path) -> str | None:
    """Motivo por el que el recibo /3 NO describe el resultado vivo; ``None`` si lo describe (o si no es un /3)."""
    if acta.get("schema") != RECEIPT_SCHEMA:
        return None
    for clave in ("reconciliation_root", "candidate_root"):
        if not Path(acta[clave]).is_absolute():
            return f"{clave} debe ser una ruta absoluta: una relativa no se puede re-acreditar sin ambigüedad"
    ruta = Path(txn_path).resolve()
    try:
        computado, _vivo = ce.evidencia_computada(ruta)
    except ce.EvidenciaError as exc:
        return f"la evidencia del cómputo no se acredita: {exc}"
    if acta["mother_computed_sha256"] != computado:
        return "el recibo describe otro cómputo de la madre (mother_computed_sha256)"
    doc, motivo = _reconciliacion(acta, ruta, estado, computado, repo_root=repo_root)
    return motivo or _candidato(acta, doc)


def _reconciliacion(
    acta: Mapping[str, str], txn_path: Path, estado: Mapping[str, Any], computado: str, *, repo_root: Path
) -> tuple[dict[str, Any], str | None]:
    raiz = Path(acta["reconciliation_root"])
    ruta = raiz / rc.RUTA_REL
    if not ruta.is_file():
        return {}, f"no hay reconciliación en {raiz}"
    if _sha(ruta) != acta["reconciliation_sha256"]:
        return {}, "la reconciliación cambió o no es la del recibo (reconciliation_sha256)"
    doc = json.loads(ruta.read_text(encoding="utf-8"))
    if doc.get("reconciliation_id") != acta["reconciliation_id"]:
        return {}, "la reconciliación no es la del recibo (reconciliation_id)"
    if doc.get("status") != "derived":
        return {}, f"la reconciliación está en {doc.get('status')!r}: sólo una `derived` describe un resultado"
    padre = doc.get("parent") or {}
    if (padre.get("campaign_id"), padre.get("campaign_json_sha256")) != (estado.get("campaign_id"), computado):
        return {}, "la reconciliación es de otra madre o de otro cómputo suyo"
    ok, salida = verificar_reconciliacion(raiz, txn_path.parents[2], repo_root=repo_root)
    if not ok:
        return {}, f"la reconciliación no se re-acredita con su lector: {salida.strip()[-500:]}"
    return doc, None


def _candidato(acta: Mapping[str, str], doc: Mapping[str, Any]) -> str | None:
    raiz = Path(acta["candidate_root"])
    head, arbol = _git(raiz, "rev-parse", "HEAD"), arbol_de(raiz, "HEAD")
    if head is None or arbol is None:
        return f"el candidato {raiz} no es un repositorio git legible"
    if (head.strip(), arbol) != (acta["candidate_git_sha"], acta["candidate_tree_sha"]):
        return "el candidato no es el commit revisado (candidate_git_sha/candidate_tree_sha)"
    sucio = _git(raiz, "status", "--porcelain", "--untracked-files=all")
    if sucio is None or sucio.strip():
        return f"el árbol del candidato está sucio de verdad: {(sucio or '').split()[:6]}"
    manifiesto = raiz / MANIFIESTO_RELEASE
    if not manifiesto.is_file() or _sha(manifiesto) != acta["release_manifest_sha256"]:
        return "el manifiesto de release del candidato no es el revisado (release_manifest_sha256)"
    if json.loads(manifiesto.read_text(encoding="utf-8")).get("release_id") != acta["release_id"]:
        return "el manifiesto de release no declara el release_id del recibo"
    return _reportes(raiz, doc)


def _reportes(raiz: Path, doc: Mapping[str, Any]) -> str | None:
    """Cada reporte versionado del candidato es el de la reconciliación (capa, fuente o salida); no sobra ninguno."""
    esperado: dict[str, str] = {
        **doc["inventory"],
        **doc["mother_layers"],
        **{rel: sha for s in doc["sources"] for rel, sha in s["artifacts"].items()},
        **(doc.get("outputs") or {}),
    }
    listado = _git(raiz, "ls-files", "-z", "--", "reports") or ""
    versionados = [r for r in listado.split("\0") if r]
    sobran = [r for r in versionados if r not in CORTE and (r.startswith(_INTERNOS) or r not in esperado)]
    distintos = [r for r in versionados if r not in CORTE and r not in sobran and _sha(raiz / r) != esperado[r]]
    # ★ auditoría 17-oct: el inventario COMPLETO previsto (capas, fuentes, salidas), salvo lo que el contrato de
    # exportación destina a otro sitio; y lo destinado a otro sitio no se cuela en datos.
    previsto = {r for r in esperado if not r.startswith(_INTERNOS) and r not in CORTE and r not in EXPORTACION}
    faltan = sorted(previsto - set(versionados))
    sobran += [r for r in versionados if r in EXPORTACION]
    if sobran or distintos or faltan:
        return (f"el candidato no es la reconciliación: distintos {distintos[:4]}, sobran {sobran[:4]}, "
                f"faltan {faltan[:4]}")  # fmt: skip
    return None
