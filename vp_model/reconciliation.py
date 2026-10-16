"""M74-F · G7 — recibo de RECONCILIACIÓN: la madre y sus complementarias, en un mismo árbol.

La cuarta campaña (`rederiv_953cd00_20261006T073927`) quedó en `computed` y cada hueco se cerró con una
complementaria en su PROPIO worktree: C3 (conformal), C4 (E3), C5c (CRPS campeón) y C6 (CRPS de
referencia). Los consumidores sólo acreditan una transacción del MISMO árbol, y el veredicto campeón sólo
se lee dentro de una campaña `running`: ninguna cifra corregida podía llegar a un canónico.

**Qué es.** Un `reports/campaign/reconciliation.json` en un worktree DESECHABLE con el código que va a
re-derivar (HEAD limpio fuera de `reports/`), el panel y la DuckDB de la madre, y tres clases de archivo:

* **capas de la madre**: los miembros regulares bajo `reports/` del tar sellado de su evidencia, extraídos
  con su hash; los AppleDouble (``._*``) y lo que queda fuera de `reports/` se declaran excluidos;
* **artefactos de las fuentes**: lo que cada complementaria `accredited` respalda (`artefactos_de`),
  copiado de su árbol tras re-acreditarla con su lector (`complement.acreditar`; C4 además se re-puntúa y
  su `e3_cohort_campaign.json` tiene que salir idéntico). Reemplazan al miembro de la madre de esa ruta;
* **referencias**: lo que la reconciliación re-deriva (`SALIDAS`) nunca se toma de nadie; si la madre lo
  traía, se guarda aparte (`REFERENCIA_REL`) para quien lo necesite como entrada (el veredicto campeón); si lo
  traía el propio árbol (`key_facts` versionado), se aparta de su ruta (`PREVIAS_REL`), de modo que al cerrar
  cada salida existe sólo si se re-derivó.

Más el inventario de TODO `reports/` al ensamblar: después sólo pueden cambiar las salidas declaradas.

**Transiciones**: ``assembled → derived`` (`cerrar` sella el hash de las salidas); cualquiera ``→ failed``,
terminal. ★ La acreditación vive en la LECTURA: `acreditar` re-verifica código, panel, DuckDB, madre,
evidencia, cada capa contra el tar, cada fuente con su lector, el inventario y las salidas, sin creer el
``status`` escrito.

**Lo que NO acredita.** `derived` no autoriza propagar ni publicar. Los constructores de canónicos que son
stages DVC (`build_key_facts`, `build_e5_facts`) derivan de archivos versionados sin transacción: la
acreditación ocurre antes, en quien escribe lo que leen (el veredicto, vía `champion.crps_provenance`), y
después, en este lector, que exige que nada salvo las salidas cambie.
"""

from __future__ import annotations

import hashlib
import json
import re
import tarfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vp_model import artifact_receipt as ar
from vp_model import complement as cx

SCHEMA = "campaign-reconciliation/1"
RUTA_REL = Path("reports") / "campaign" / "reconciliation.json"
#: Donde se guardan, con su ruta original debajo, los miembros de la madre que la reconciliación re-deriva.
REFERENCIA_REL = "reports/campaign/reconciliation/madre"
#: Donde se apartan las salidas que el ÁRBOL ya traía al ensamblar (p. ej. `key_facts` versionado): retiradas de su
#: ruta, que exista una salida al cerrar prueba que se produjo después de ensamblar (auditoría 15-oct).
PREVIAS_REL = "reports/campaign/reconciliation/arbol"
#: El informe que puntúa E3: C4 lo respalda porque su lector lo re-calcula idéntico desde los recibos.
E3_REL = "reports/eval/e3_cohort_campaign.json"
#: Lo que la reconciliación re-deriva con el código del árbol: jamás una capa ni un artefacto de una fuente.
SALIDAS = (
    "reports/governance/champion_challenger.json",
    "reports/governance/champion_challenger.md",
    "reports/governance/key_facts.json",
    "reports/latex/key_facts.tex",
    "reports/eval/horizon_facts.json",
    "reports/latex/horizon_champion.tex",
    "reports/governance/e5_facts.json",
    "reports/latex/cohorts_facts.tex",
    "reports/latex/cohortes.tex",
    "reports/prospective/forecast_vs_actual_FAD.csv",
    "reports/prospective/forecast_vs_actual_DFF.csv",
)
#: Figuras del `.tex` (salida local ignorada por git desde M66), por productor: `make_result_figures` (8),
#: `make_hero_figures` (2), `make_horizon_figure` y `make_cohort_figure`.
FIGURAS = "reports/latex/Figures/"
FIGURAS_PDF = ("results_ranking_mase", "results_forecast_winner", "results_multiseed_ci", "results_coverage_crps",
               "results_backtest_grid_FAD", "results_backtest_grid_DFF", "results_cd_diagram", "results_error_heatmap",
               "results_hero_forecast", "results_hero_philippines", "horizon_mase_curves", "e5_router_effect")  # fmt: skip
#: G7 · B2: TODAS las figuras que la re-derivación produce, por nombre exacto (no por carpeta: una figura que nadie
#: declara no es una salida), incluidas las que caen fuera de `FIGURAS`: las variantes PNG de `make_cohort_figure`,
#: la de hold-out de `vp_model.report --figure-only` y el CD del paper de `significance_tables --cd-only`.
SALIDAS_FIGURA = (
    *(f"{FIGURAS}{n}.pdf" for n in FIGURAS_PDF),
    *(f"reports/figures/cohorts/{d}e5_router_effect.png" for d in ("", "dark/", "en/", "en/dark/")),
    "reports/figures/results_holdout_mexico_F3_FAD.png",
    "reports/paper_micai/Figures/fig_cd_diagram.pdf",
)
#: Basura del sistema que no es ni entrada ni salida.
IGNORADOS = frozenset({".DS_Store"})
TRANSICIONES: dict[str, frozenset[str]] = {
    "assembled": frozenset({"derived", "failed"}),
    "derived": frozenset({"failed"}),
    "failed": frozenset(),
}
LEIBLES = frozenset({"assembled", "derived"})
_ID = re.compile(r"^recon_[A-Za-z0-9_.-]{1,120}$")
_PLAN = ("schema", "reconciliation_id", "parent", "code", "panel_sha256", "duckdb", "evidence", "sources",
         "mother_layers", "references", "previous_outputs", "excluded", "inventory")  # fmt: skip

Problema = cx.Problema
Git = cx.Git
#: ``(raíz de la fuente E3, raíz de la madre) -> informe``: el puntuador real (`score_e3_campaign.puntuar`).
PuntuarE3 = Callable[[Path, Path], Mapping[str, Any]]


class ReconciliacionError(RuntimeError):
    """La reconciliación no se acredita. ``codigos`` permite a una prueba distinguir el motivo."""

    def __init__(self, problemas: Sequence[Problema]) -> None:
        self.problemas = list(problemas)
        self.codigos = {c for c, _ in self.problemas}
        super().__init__(f"{len(self.problemas)} problema(s):\n  " + "\n  ".join(f"[{c}] {m}" for c, m in problemas))


def _ahora() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def es_salida(rel: str) -> bool:
    return rel in SALIDAS or rel in SALIDAS_FIGURA


# ═══════════════════════════════ fuentes: lo que cada complementaria respalda
def artefactos_de(src: Path, doc: Mapping[str, Any]) -> list[str]:
    """Las rutas que una complementaria acreditada respalda en su árbol, según su propósito.

    E3: el informe puntuado, los recibos de sus lanes y las salidas que esos recibos hashean (que su lector
    acaba de verificar). C3: las salidas de conformal y su sidecar. C5/C6: el CSV de CRPS y su recibo.
    """
    proposito = doc["purpose"]
    if proposito in cx.SALIDA_CRPS:
        csv_rel = cx.SALIDA_CRPS[proposito]
        return [csv_rel, str(ar.receipt_path(Path(csv_rel)))]
    if proposito == "deep_pi_conformal":
        return [cx.CONFORMAL_REL, cx.GAMMA_REL, cx.PROCEDENCIA_REL]
    if proposito == "e3":
        recibos = sorted((Path(src) / doc["lanes_dir"]).glob("receipt_*.json"))
        salidas = [str(o["path"]) for r in recibos for o in json.loads(r.read_text(encoding="utf-8"))["outputs"]]
        return [E3_REL, *(str(r.relative_to(src)) for r in recibos), *salidas]
    raise ReconciliacionError([("fuente-proposito", f"propósito {proposito!r} sin artefactos declarados")])


def _ruta_valida(rel: str) -> bool:
    partes = Path(rel).parts
    return bool(partes) and partes[0] == cx.DATOS_REL and ".." not in partes and not Path(rel).is_absolute()


def _acreditar_fuente(
    src: Path,
    parent_root: Path,
    *,
    acreditar_lanes: cx.AcreditarLanes | None,
    puntuar_e3: PuntuarE3 | None,
    git: Git,
) -> tuple[dict, list[Problema]]:
    """La complementaria de ``src`` con SU lector; para E3, además, el informe re-puntuado idéntico."""
    try:
        doc = cx.leer(src)
    except cx.ComplementoError as exc:
        return {}, [("fuente-ilegible", f"{src}: {exc}")]
    cid = doc["complement_id"]
    p: list[Problema] = []
    if doc["status"] != "accredited":
        p.append(("fuente-estado", f"{cid} está en {doc['status']!r}; sólo una 'accredited' es fuente"))
    if doc["lanes"] and acreditar_lanes is None:
        return doc, [*p, ("fuente-lector", f"{cid} tiene lanes y no se dio su acreditador")]
    try:
        cx.acreditar(src, parent_root=parent_root, acreditar_lanes=acreditar_lanes or _sin_lanes, git=git)
    except cx.ComplementoError as exc:
        p.append(("fuente-no-acreditada", f"{cid}: {exc}"))
    if doc["purpose"] == "e3" and not p:
        p += _repuntuar_e3(src, parent_root, cid, puntuar_e3)
    return doc, p


def _sin_lanes(*_a: object, **_k: object) -> None:
    raise RuntimeError("esta fuente no tiene lanes que acreditar")


def _repuntuar_e3(src: Path, parent_root: Path, cid: str, puntuar_e3: PuntuarE3 | None) -> list[Problema]:
    if puntuar_e3 is None:
        return [("fuente-lector", f"{cid}: E3 sin puntuador con que re-calcular su informe")]
    try:
        escrito = ar.loads_strict((Path(src) / E3_REL).read_text(encoding="utf-8"))
        rehecho = json.loads(json.dumps(dict(puntuar_e3(Path(src), Path(parent_root))), sort_keys=True))
    except (OSError, ValueError, RuntimeError) as exc:
        return [("fuente-e3", f"{cid}: no se pudo re-puntuar E3: {exc}")]
    if escrito != rehecho:
        distintas = sorted(k for k in set(escrito) | set(rehecho) if escrito.get(k) != rehecho.get(k))
        return [("fuente-e3", f"{cid}: {E3_REL} no es el que dan sus recibos (difieren {distintas[:5]})")]
    return []


def _sello_fuente(src: Path, doc: Mapping[str, Any]) -> tuple[dict, list[Problema]]:
    """Lo que el plan sella de una fuente: identidad, hash de su transacción y de cada artefacto."""
    cid = doc["complement_id"]
    artefactos: dict[str, str] = {}
    p: list[Problema] = []
    for rel in artefactos_de(src, doc):
        f = Path(src) / rel
        if not _ruta_valida(rel) or es_salida(rel):
            p.append(("fuente-ruta", f"{cid}: {rel} no es una ruta de datos que la fuente pueda aportar"))
        elif not f.is_file():
            p.append(("fuente-artefacto", f"{cid}: falta {rel} en su árbol"))
        else:
            artefactos[rel] = ar.sha256_file(f)
    return {
        "root": str(Path(src).resolve()),
        "complement_id": cid,
        "purpose": doc["purpose"],
        "complement_json_sha256": ar.sha256_file(Path(src) / cx.RUTA_REL),
        "code_sha": doc["code"]["source_git_sha"],
        "duckdb_catalog_sha256": (doc.get("duckdb") or {}).get("catalog_sha256"),
        "frozen_reports": {k: v for k, v in doc["inputs"].items() if k.startswith(f"{cx.DATOS_REL}/")},
        "artifacts": artefactos,
    }, p


def _coherencia_fuente(
    sello: Mapping[str, Any], doc: Mapping[str, Any], padre: Mapping[str, str], root: Path, catalogo: str | None
) -> list[Problema]:
    """La fuente es de ESTA madre y comparte con el árbol el catálogo SQL y las entradas de datos congeladas."""
    cid = sello["complement_id"]
    p: list[Problema] = []
    if (doc["parent"]["campaign_id"], doc["parent"]["campaign_json_sha256"]) != (
        padre["campaign_id"],
        padre["campaign_json_sha256"],
    ):
        p.append(("fuente-otra-madre", f"{cid} es complementaria de otra madre o de otra revisión suya"))
    if doc["panel_sha256"] != padre["panel_sha256"]:
        p.append(("fuente-panel", f"{cid}: panel distinto del de la madre"))
    if sello["duckdb_catalog_sha256"] != catalogo:
        p.append(("fuente-duckdb", f"{cid}: su DuckDB tiene otro catálogo SQL que la del árbol"))
    for rel, sha in sorted(sello["frozen_reports"].items()):
        f = Path(root) / rel
        if not f.is_file() or ar.sha256_file(f) != sha:
            p.append(("fuente-entrada", f"{cid}: {rel} del árbol no es la entrada congelada de la fuente"))
    return p


# ═══════════════════════════════ madre: miembros del tar de su evidencia
def _miembros(archivo: Path) -> tuple[dict[str, str], list[dict], list[Problema]]:
    """``(capas posibles, excluidos, problemas)`` del tar: un miembro regular y único por ruta bajo `reports/`."""
    vistos: dict[str, list[tuple[bool, str | None]]] = {}
    excluidos: list[dict] = []
    with tarfile.open(archivo, "r:*") as tar:
        for m in tar:
            nombre = m.name.removeprefix("./")
            if m.isdir():
                continue
            if Path(nombre).name.startswith("._"):
                excluidos.append({"member": nombre, "reason": "metadatos AppleDouble de macOS"})
                continue
            if not _ruta_valida(nombre):
                excluidos.append({"member": nombre, "reason": f"fuera de {cx.DATOS_REL}/: no es una salida de datos"})
                continue
            fh = tar.extractfile(m) if m.isreg() else None
            vistos.setdefault(nombre, []).append((m.isreg(), hashlib.sha256(fh.read()).hexdigest() if fh else None))
    capas: dict[str, str] = {}
    p: list[Problema] = []
    for nombre, h in sorted(vistos.items()):
        if len(h) > 1:
            p.append(("madre-duplicado", f"{nombre}: {len(h)} miembros con ese nombre"))
        elif not h[0][0] or h[0][1] is None:
            p.append(("madre-no-regular", f"{nombre}: el miembro no es un archivo regular"))
        else:
            capas[nombre] = h[0][1]
    return capas, excluidos, p


def _leer_miembros(archivo: Path, nombres: set[str]) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    with tarfile.open(archivo, "r:*") as tar:
        for m in tar:
            nombre = m.name.removeprefix("./")
            fh = tar.extractfile(m) if m.isreg() and nombre in nombres else None
            if fh is not None:
                out[nombre] = fh.read()
    return out


# ═══════════════════════════════ árbol: código, panel, DuckDB, inventario
def inventario(root: Path) -> dict[str, str]:
    """Hash de cada archivo bajo `reports/`, salvo el propio recibo y la basura del sistema."""
    base = Path(root) / cx.DATOS_REL
    out: dict[str, str] = {}
    for f in sorted(base.rglob("*")):
        rel = str(f.relative_to(root))
        if f.is_file() and f.name not in IGNORADOS and rel != str(RUTA_REL):
            out[rel] = ar.sha256_file(f)
    return out


def _otras_transacciones(root: Path) -> list[Problema]:
    """Una campaña o una complementaria en el árbol desviarían a los consumidores hacia ellas."""
    return [
        ("transaccion-ajena", f"{rel} en el árbol: una reconciliación vive sola")
        for rel in (cx.CAMPANA_REL, cx.RUTA_REL)
        if (Path(root) / rel).exists()
    ]


def _arbol(root: Path, panel: str, sellado: str | None, git: Git) -> tuple[str, dict | None, list[Problema]]:
    """``(HEAD, sello de la DuckDB, problemas)`` del árbol: código permitido, panel y DuckDB de la madre."""
    head, p = cx.codigo_permitido(root, sellado, git)
    p += _otras_transacciones(root)
    if cx._panel(root) != panel:
        p.append(("panel", "el panel en disco no es el que selló la madre"))
    try:
        duck = cx.sello_duckdb(Path(root) / cx.DUCKDB_REL)
    except cx.ComplementoError as exc:
        return head, None, p + exc.problemas
    return head, duck, p + cx._problemas_duckdb(duck, panel)


# ═══════════════════════════════ ensamblar
def ensamblar(
    root: Path,
    *,
    parent_root: Path,
    evidence: str,
    sources: Sequence[Path],
    reconciliation_id: str,
    acreditar_lanes: cx.AcreditarLanes | None = None,
    puntuar_e3: PuntuarE3 | None = None,
    git: Git = cx.estado_git,
    ahora: Callable[[], str] = _ahora,
) -> dict:
    """Comprueba TODO antes de escribir; luego extrae las capas, copia los artefactos y sella el plan."""
    root = Path(root)
    if (root / RUTA_REL).exists():
        raise ReconciliacionError([("ya-ensamblada", f"{RUTA_REL} ya existe: un reintento es otra reconciliación")])
    padre = _padre(parent_root)
    p: list[Problema] = (
        [] if _ID.match(reconciliation_id) else [("id", f"{reconciliation_id!r} no cumple {_ID.pattern}")]
    )
    head, duck, problemas_arbol = _arbol(root, padre["panel_sha256"], None, git)
    p += problemas_arbol
    evidencia: dict = {}
    try:
        evidencia = cx.acreditar_evidencia(root, evidence, padre)
    except cx.ComplementoError as exc:
        p += exc.problemas
    sellos = []
    for src in sources:
        doc, pf = _acreditar_fuente(Path(src), Path(parent_root), acreditar_lanes=acreditar_lanes,
                                    puntuar_e3=puntuar_e3, git=git)  # fmt: skip
        if doc and not pf:
            sello, pf = _sello_fuente(Path(src), doc)
            pf += _coherencia_fuente(sello, doc, padre, root, (duck or {}).get("catalog_sha256"))
            sellos.append(sello)
        p += pf
    p += _fuentes_unicas(sellos)
    if p or duck is None:
        raise ReconciliacionError(p or [("duckdb-ausente", "sin DuckDB sellada")])
    try:
        capas, excluidos, p = _miembros(root / evidencia["archive"]["path"])
    except (tarfile.TarError, OSError) as exc:
        raise ReconciliacionError([("evidencia-archivo", f"tar ilegible: {exc}")]) from exc
    if p:
        raise ReconciliacionError(p)
    doc = _plan(root, padre, head, duck, evidencia, sellos, capas, excluidos, reconciliation_id)
    doc.update(plan_sha256=cx.huella({k: doc[k] for k in _PLAN}), status="assembled", revision=1,
               timestamps={"assembled_at": ahora()}, failure=None, outputs=None)  # fmt: skip
    _escribir(root, doc)
    return doc


def _padre(parent_root: Path) -> dict[str, str]:
    try:
        return cx.leer_padre(parent_root)
    except cx.ComplementoError as exc:
        raise ReconciliacionError(exc.problemas) from exc


def _fuentes_unicas(sellos: Sequence[Mapping[str, Any]]) -> list[Problema]:
    p: list[Problema] = []
    propositos = [s["purpose"] for s in sellos]
    for prop in sorted({x for x in propositos if propositos.count(x) > 1}):
        p.append(("fuente-repetida", f"dos fuentes de propósito {prop!r}: ¿cuál responde por sus artefactos?"))
    rutas = [rel for s in sellos for rel in s["artifacts"]]
    for rel in sorted({x for x in rutas if rutas.count(x) > 1}):
        p.append(("fuente-solapada", f"{rel} lo aportan dos fuentes"))
    return p


def _plan(
    root: Path,
    padre: Mapping[str, str],
    head: str,
    duck: Mapping[str, Any],
    evidencia: Mapping[str, Any],
    sellos: Sequence[Mapping[str, Any]],
    capas: Mapping[str, str],
    excluidos: Sequence[Mapping[str, str]],
    reconciliation_id: str,
) -> dict:
    """Escribe capas, referencias y artefactos de las fuentes y devuelve el plan (sin estado)."""
    de_fuentes = {rel for s in sellos for rel in s["artifacts"]}
    referencias = {
        f"{REFERENCIA_REL}/{rel}": {"member": rel, "sha256": sha} for rel, sha in capas.items() if es_salida(rel)
    }
    capa = {rel: sha for rel, sha in capas.items() if not es_salida(rel) and rel not in de_fuentes}
    sustituidos = sorted(rel for rel in capas if rel in de_fuentes)
    previas = _apartar_previas(root)
    datos = _leer_miembros(root / evidencia["archive"]["path"], set(capas))
    for rel in capa:
        _volcar(root / rel, datos[rel])
    for ref, m in referencias.items():
        _volcar(root / ref, datos[m["member"]])
    for s in sellos:
        for rel in s["artifacts"]:
            _volcar(root / rel, (Path(s["root"]) / rel).read_bytes())
    return {
        "schema": SCHEMA,
        "reconciliation_id": reconciliation_id,
        "parent": dict(padre),
        "code": {"source_git_sha": head},
        "panel_sha256": padre["panel_sha256"],
        "duckdb": dict(duck),
        "evidence": dict(evidencia),
        "sources": [dict(s) for s in sellos],
        "mother_layers": capa,
        "references": referencias,
        "previous_outputs": previas,
        "excluded": [*excluidos, *({"member": rel, "reason": "lo sustituye una fuente"} for rel in sustituidos)],
        "inventory": inventario(root),
    }


def _apartar_previas(root: Path) -> dict[str, dict[str, str]]:
    """Mueve a `PREVIAS_REL` cada salida declarada que el árbol ya traía: sólo así ``cerrar`` distingue una
    salida re-derivada de una vieja que nadie volvió a producir."""
    previas: dict[str, dict[str, str]] = {}
    for rel, sha in inventario(root).items():
        if es_salida(rel):
            ref = f"{PREVIAS_REL}/{rel}"
            _volcar(Path(root) / ref, (Path(root) / rel).read_bytes())
            (Path(root) / rel).unlink()
            previas[ref] = {"path": rel, "sha256": sha}
    return previas


def _volcar(destino: Path, datos: bytes) -> None:
    destino.parent.mkdir(parents=True, exist_ok=True)
    ar.replace_atomic(destino, lambda t: t.write_bytes(datos))


def _escribir(root: Path, doc: Mapping[str, Any]) -> None:
    texto = json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ar.replace_atomic(Path(root) / RUTA_REL, lambda t: t.write_text(texto, encoding="utf-8"))


# ═══════════════════════════════ lector
def leer(root: Path) -> dict:
    """El documento, con el plan re-verificado contra su huella."""
    ruta = Path(root) / RUTA_REL
    if not ruta.is_file():
        raise ReconciliacionError([("ausente", f"no hay reconciliación en {ruta}")])
    try:
        doc = ar.loads_strict(ruta.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ReconciliacionError([("ilegible", f"{ruta}: {exc}")]) from exc
    if doc.get("schema") != SCHEMA:
        raise ReconciliacionError([("esquema", f"esquema {doc.get('schema')!r}")])
    if any(k not in doc for k in _PLAN) or cx.huella({k: doc[k] for k in _PLAN}) != doc.get("plan_sha256"):
        raise ReconciliacionError([("plan-editado", "el plan no coincide con la huella sellada al ensamblar")])
    if doc.get("status") not in TRANSICIONES:
        raise ReconciliacionError([("estado", f"estado desconocido {doc.get('status')!r}")])
    return doc


def _capas_contra_tar(root: Path, doc: Mapping[str, Any]) -> list[Problema]:
    """Cada capa y referencia, contra el miembro del tar sellado que dice ser."""
    ev = doc["evidence"]
    p: list[Problema] = []
    for clave in ("manifest", "identity", "archive"):
        f = Path(root) / ev[clave]["path"]
        if not f.is_file() or ar.sha256_file(f) != ev[clave]["sha256"]:
            p.append(("evidencia-cambio", f"{ev[clave]['path']}: falta o cambió desde el ensamblado"))
    if p:
        return p
    try:
        capas, _excluidos, p = _miembros(Path(root) / ev["archive"]["path"])
    except (tarfile.TarError, OSError) as exc:
        return [("evidencia-archivo", f"tar ilegible: {exc}")]
    esperadas = {**doc["mother_layers"], **{m["member"]: m["sha256"] for m in doc["references"].values()}}
    for rel, sha in sorted(esperadas.items()):
        if capas.get(rel) != sha:
            p.append(("capa-tar", f"{rel}: no es el miembro del tar sellado de la madre"))
    return p


def _archivos(root: Path, doc: Mapping[str, Any]) -> list[Problema]:
    """Capas, referencias y artefactos en disco = lo sellado; el resto de `reports/` = el inventario."""
    sellados = {
        **doc["mother_layers"],
        **{ref: m["sha256"] for ref, m in doc["references"].items()},
        **{ref: m["sha256"] for ref, m in doc["previous_outputs"].items()},
        **{rel: sha for s in doc["sources"] for rel, sha in s["artifacts"].items()},
    }
    autoridades = [*doc["mother_layers"], *doc["references"], *doc["previous_outputs"],
                   *(rel for s in doc["sources"] for rel in s["artifacts"])]  # fmt: skip
    p = [("plan-solapado", f"{rel}: el plan le da más de una autoridad (madre, referencia o fuente)")
         for rel in sorted({r for r in autoridades if autoridades.count(r) > 1})]  # fmt: skip
    vivo = inventario(root)
    p += [("capa-cambio", f"{rel}: falta o cambió desde el ensamblado") for rel, sha in sorted(sellados.items())
          if vivo.get(rel) != sha]  # fmt: skip
    salidas = doc.get("outputs") or {}
    for rel in sorted(set(vivo) | set(doc["inventory"]) | set(salidas)):  # una salida sellada que falta, también
        if rel in sellados:
            continue
        if es_salida(rel):  # al cerrar se sellan TODAS las declaradas: después sólo pueden cambiar o faltar
            if rel in salidas and vivo.get(rel) != salidas[rel]:
                p.append(("salida-cambio", f"{rel}: cambió desde que se cerró la reconciliación"))
        elif vivo.get(rel) != doc["inventory"].get(rel):
            p.append(("inventario", f"{rel}: cambió, apareció o desapareció y no es una salida declarada"))
    return p


def _fuentes(
    doc: Mapping[str, Any],
    parent_root: Path,
    *,
    solo: str | None,
    acreditar_lanes: cx.AcreditarLanes | None,
    puntuar_e3: PuntuarE3 | None,
    git: Git,
) -> list[Problema]:
    """Cada fuente (o sólo ``solo``) re-acreditada en su árbol, con sus artefactos aún en él."""
    elegidas = [s for s in doc["sources"] if solo is None or s["complement_id"] == solo]
    if solo is not None and not elegidas:
        return [("fuente-ausente", f"{solo} no es fuente de esta reconciliación")]
    p: list[Problema] = []
    for s in elegidas:
        src = Path(s["root"])
        fuente, pf = _acreditar_fuente(src, parent_root, acreditar_lanes=acreditar_lanes, puntuar_e3=puntuar_e3,
                                       git=git)  # fmt: skip
        if fuente and ar.sha256_file(src / cx.RUTA_REL) != s["complement_json_sha256"]:
            pf.append(("fuente-cambio", f"{s['complement_id']}: su complement.json cambió desde el ensamblado"))
        for rel, sha in sorted(s["artifacts"].items()):
            if not (src / rel).is_file() or ar.sha256_file(src / rel) != sha:
                pf.append(("fuente-artefacto", f"{s['complement_id']}: {rel} cambió en su árbol"))
        p += pf
    return p


def acreditar(
    root: Path,
    *,
    parent_root: Path,
    solo: str | None = None,
    acreditar_lanes: cx.AcreditarLanes | None = None,
    puntuar_e3: PuntuarE3 | None = None,
    git: Git = cx.estado_git,
) -> dict:
    """LECTOR: re-verifica todo sin creer el estado escrito. No escribe.

    ``solo`` limita la re-acreditación de FUENTES a la complementaria de ese id (un consumidor de CRPS no
    necesita re-puntuar E3); el árbol, la madre, las capas y el inventario se verifican siempre enteros.
    """
    root = Path(root)
    doc = leer(root)
    if doc["status"] not in LEIBLES:
        raise ReconciliacionError([("estado", f"una reconciliación en {doc['status']!r} no se consume")])
    p = _verificar(root, doc, parent_root, solo=solo, acreditar_lanes=acreditar_lanes, puntuar_e3=puntuar_e3,
                   git=git)  # fmt: skip
    if p:
        raise ReconciliacionError(p)
    return {"reconciliation_id": doc["reconciliation_id"], "status": doc["status"],
            "parent": doc["parent"]["campaign_id"], "sources": [s["complement_id"] for s in doc["sources"]]}  # fmt: skip


def _verificar(
    root: Path,
    doc: Mapping[str, Any],
    parent_root: Path,
    *,
    solo: str | None,
    acreditar_lanes: cx.AcreditarLanes | None,
    puntuar_e3: PuntuarE3 | None,
    git: Git,
) -> list[Problema]:
    _head, duck, p = _arbol(root, doc["panel_sha256"], doc["code"]["source_git_sha"], git)
    if duck is not None and (duck["content_sha256"], duck["catalog_sha256"]) != (
        doc["duckdb"]["content_sha256"],
        doc["duckdb"]["catalog_sha256"],
    ):
        p.append(("duckdb-cambio", "la DuckDB del árbol no es la sellada al ensamblar"))
    try:
        if cx.leer_padre(parent_root)["campaign_json_sha256"] != doc["parent"]["campaign_json_sha256"]:
            p.append(("padre-cambio", "el campaign.json de la madre cambió desde el ensamblado"))
    except cx.ComplementoError as exc:
        p += exc.problemas
    p += _capas_contra_tar(root, doc) + _archivos(root, doc)
    return p + _fuentes(doc, Path(parent_root), solo=solo, acreditar_lanes=acreditar_lanes, puntuar_e3=puntuar_e3,
                        git=git)  # fmt: skip


# ═══════════════════════════════ ciclo de vida
def _mover(root: Path, doc: dict, nuevo: str, ahora: Callable[[], str], **campos: Any) -> dict:
    if nuevo not in TRANSICIONES[doc["status"]]:
        raise ReconciliacionError([("transicion", f"{doc['status']} → {nuevo} no es una transición admitida")])
    doc = {**doc, **campos, "status": nuevo, "revision": int(doc["revision"]) + 1}
    doc["timestamps"] = {**doc["timestamps"], f"{nuevo}_at": ahora()}
    _escribir(root, doc)
    return doc


def cerrar(
    root: Path,
    *,
    parent_root: Path,
    acreditar_lanes: cx.AcreditarLanes | None = None,
    puntuar_e3: PuntuarE3 | None = None,
    git: Git = cx.estado_git,
    ahora: Callable[[], str] = _ahora,
) -> dict:
    """``assembled → derived``: todo se re-acredita, están TODAS las `SALIDAS` y se sella su hash.

    Una comprobación que falla deja la reconciliación en ``failed`` (terminal: un reintento es otra)."""
    root = Path(root)
    doc = leer(root)
    if doc["status"] not in LEIBLES:
        raise ReconciliacionError([("estado", f"una reconciliación en {doc['status']!r} no se cierra")])
    p = _verificar(root, doc, parent_root, solo=None, acreditar_lanes=acreditar_lanes, puntuar_e3=puntuar_e3,
                   git=git)  # fmt: skip
    vivo = inventario(root)
    p += [("salida-ausente", f"{rel}: la reconciliación no la re-derivó")
          for rel in (*SALIDAS, *SALIDAS_FIGURA) if rel not in vivo]  # fmt: skip
    if p:
        _mover(root, doc, "failed", ahora, failure={"stage": "cerrar", "codes": sorted({c for c, _ in p})})
        raise ReconciliacionError(p)
    salidas = {rel: sha for rel, sha in vivo.items() if es_salida(rel)}
    return _mover(root, doc, "derived", ahora, outputs=salidas)


def fallar(root: Path, motivo: str, *, ahora: Callable[[], str] = _ahora) -> dict:
    """Aborto explícito. Terminal."""
    doc = leer(Path(root))
    return _mover(Path(root), doc, "failed", ahora, failure={"stage": "abortada", "codes": [], "reason": motivo})


def referencia_previa(root: Path, rel: str) -> Path:
    """La versión que el árbol traía de una salida antes de ensamblar, apartada de su ruta."""
    doc = leer(Path(root))
    ref = f"{PREVIAS_REL}/{rel}"
    if ref not in doc["previous_outputs"]:
        raise ReconciliacionError([("referencia-ausente", f"el árbol no traía {rel}")])
    return Path(root) / ref


def referencia(root: Path, rel: str) -> Path:
    """La copia de la madre de una salida que la reconciliación re-deriva (p. ej. el veredicto campeón)."""
    doc = leer(Path(root))
    ref = f"{REFERENCIA_REL}/{rel}"
    if ref not in doc["references"]:
        raise ReconciliacionError([("referencia-ausente", f"la madre no aportó {rel}")])
    return Path(root) / ref
