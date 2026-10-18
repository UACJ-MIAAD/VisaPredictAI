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
  versionado es byte a byte el de la reconciliación —salvo los dos artefactos del corte—, sin sobrar ninguno. Todo se
  lee del COMMIT revisado (modo y blob), nunca del disco: un enlace simbólico o un archivo oculto con
  `assume-unchanged` harían acreditar bytes que el commit no contiene (B5a-3). Y git no sigue reemplazos locales
  (`--no-replace-objects`): un `refs/replace` haría leer otro commit, árbol o blob.

`publish` añade la última pieza: el commit que se publica tiene EXACTAMENTE el árbol revisado (`arbol_de`).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Iterable, Mapping
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
#: Modo git del único tipo de entrada publicable: archivo regular no ejecutable. Un enlace (120000) publica una ruta y
#: no los bytes a los que apunta; un submódulo (160000), otro repositorio; un ejecutable (100755), otro árbol.
MODO_REGULAR = "100644"
#: Toda consulta al candidato lee los objetos REALES: un `refs/replace` local hace que git devuelva otro commit, árbol
#: o blob en lugar del que el commit contiene (auditoría 17-oct, B5a-3).
_GIT = ("git", "--no-replace-objects")
#: Lo que la reconciliación guarda para sí: jamás viaja al candidato.
_INTERNOS = ("reports/campaign/reconciliation/", "reports/campaign/complement_evidence/", str(rc.RUTA_REL))


def _sha(ruta: Path) -> str:
    return hashlib.sha256(ruta.read_bytes()).hexdigest()


def _git(raiz: Path, *args: str) -> str | None:
    fin = subprocess.run([*_GIT, "-C", str(raiz), *args], capture_output=True, text=True, check=False)
    return fin.stdout if fin.returncode == 0 else None


def _entradas(raiz: Path, commit: str) -> dict[str, tuple[str, str]] | None:
    """`reports/` del COMMIT: ruta → (modo, oid), leído del árbol de git y no del disco (``None`` si no se lee)."""
    fin = subprocess.run([*_GIT, "-C", str(raiz), "ls-tree", "-r", "-z", "--full-tree", commit, "--", "reports"],
                         capture_output=True, check=False)  # fmt: skip
    if fin.returncode != 0:
        return None
    entradas: dict[str, tuple[str, str]] = {}
    for linea in filter(None, fin.stdout.split(b"\0")):
        meta, _tab, ruta = linea.partition(b"\t")
        modo, _tipo, oid = meta.decode("ascii").split()
        entradas[ruta.decode("utf-8", "surrogateescape")] = (modo, oid)
    return entradas


def _blobs(raiz: Path, oids: Iterable[str]) -> dict[str, bytes] | None:
    """El contenido de cada blob en UNA lectura de git (`cat-file --batch`); ``None`` si alguno no se puede leer."""
    pedidos = sorted(set(oids))
    fin = subprocess.run([*_GIT, "-C", str(raiz), "cat-file", "--batch"], input="".join(f"{o}\n" for o in pedidos).encode(),
                         capture_output=True, check=False)  # fmt: skip
    if fin.returncode != 0:
        return None
    salida, pos, leidos = fin.stdout, 0, {}
    for oid in pedidos:
        fin_cabecera = salida.find(b"\n", pos)
        cabecera = salida[pos:fin_cabecera].decode("ascii", "replace").split() if fin_cabecera >= 0 else []
        if len(cabecera) != 3 or cabecera[0] != oid or cabecera[1] != "blob":
            return None
        inicio, tamano = fin_cabecera + 1, int(cabecera[2])
        leidos[oid] = salida[inicio : inicio + tamano]
        if len(leidos[oid]) != tamano:
            return None
        pos = inicio + tamano + 1
    return leidos


def _release_id(manifiesto: bytes) -> object:
    try:
        doc = json.loads(manifiesto.decode("utf-8"))
    except UnicodeDecodeError, ValueError:
        return None
    return doc.get("release_id") if isinstance(doc, dict) else None


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
    # ★ B5a-3: los bytes revisados son los del COMMIT. Modo y blob salen de git, no del disco.
    commit = acta["candidate_git_sha"]
    entradas = _entradas(raiz, commit)
    if entradas is None:
        return f"no se pudo leer el árbol del commit revisado {commit}"
    otros = sorted(r for r, (modo, _oid) in entradas.items() if modo != MODO_REGULAR)
    if otros:
        detalle = ", ".join(f"{r} (modo {entradas[r][0]})" for r in otros[:4])
        return f"no es un archivo regular ({MODO_REGULAR}) en el commit revisado: {detalle}"
    faltan = sorted(CORTE - set(entradas))
    if faltan:
        return f"falta en el commit revisado: {faltan}"
    blobs = _blobs(raiz, (oid for _modo, oid in entradas.values()))
    if blobs is None:
        return f"no se pudo leer algún blob del commit revisado {commit}"
    contenido = {r: blobs[oid] for r, (_modo, oid) in entradas.items()}
    manifiesto = contenido[MANIFIESTO_RELEASE]  # hash y release_id, de los MISMOS bytes
    if hashlib.sha256(manifiesto).hexdigest() != acta["release_manifest_sha256"]:
        return "el manifiesto de release del candidato no es el revisado (release_manifest_sha256)"
    if _release_id(manifiesto) != acta["release_id"]:
        return "el manifiesto de release no declara el release_id del recibo"
    return _reportes(contenido, doc)


def _reportes(contenido: Mapping[str, bytes], doc: Mapping[str, Any]) -> str | None:
    """Cada reporte del commit revisado es el de la reconciliación (capa, fuente o salida); no sobra ninguno."""
    esperado: dict[str, str] = {
        **doc["inventory"],
        **doc["mother_layers"],
        **{rel: sha for s in doc["sources"] for rel, sha in s["artifacts"].items()},
        **(doc.get("outputs") or {}),
    }
    versionados = list(contenido)
    sobran = [r for r in versionados if r not in CORTE and (r.startswith(_INTERNOS) or r not in esperado)]
    distintos = [
        r
        for r in versionados
        if r not in CORTE and r not in sobran and hashlib.sha256(contenido[r]).hexdigest() != esperado[r]
    ]
    # ★ auditoría 17-oct: el inventario COMPLETO previsto (capas, fuentes, salidas), salvo lo que el contrato de
    # exportación destina a otro sitio; y lo destinado a otro sitio no se cuela en datos.
    previsto = {r for r in esperado if not r.startswith(_INTERNOS) and r not in CORTE and r not in EXPORTACION}
    faltan = sorted(previsto - set(versionados))
    sobran += [r for r in versionados if r in EXPORTACION]
    if sobran or distintos or faltan:
        return (f"el candidato no es la reconciliación: distintos {distintos[:4]}, sobran {sobran[:4]}, "
                f"faltan {faltan[:4]}")  # fmt: skip
    return None
