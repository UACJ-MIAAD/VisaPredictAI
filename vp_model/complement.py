"""M74-F · C4/C3 — transacción COMPLEMENTARIA de una campaña ya calculada.

La cuarta campaña real (`rederiv_953cd00_20261006T073927`) quedó en `computed` con dos huecos que no
se pueden cerrar re-etiquetando lo que ya existe: los 42 recibos de E3 no llevan identidad (y los 6
GBM corrieron con la semilla 42), y conformal leyó el deep-PI de la corrida ANTERIOR. Repetir la
campaña entera son ≈ 37 h; una complementaria repite sólo lo que hace falta y deja la madre intacta.

**Qué es.** Un `reports/campaign/complement.json` en un worktree DESECHABLE (nunca en exec) con:

* identidad propia (``complement_id``), código sellado (HEAD limpio) y panel = el de la madre;
* referencia a la madre por ``campaign_id`` + sha256 de su ``campaign.json``, que sólo se LEE;
* el conjunto EXACTO de lanes y la semilla, fijados al planificar;
* importaciones explícitas (``artifact-import/1``): artefactos de la madre que se reutilizan con
  origen, hashes, panel, protocolo y evidencia, marcados ``importado`` — nunca como producidos aquí.

**Por qué un archivo aparte y no `campaign.json`.** Todo lector de campaña completa (`publishable`,
`txn validate`, `sync_all`) mira `campaign.json`. Una complementaria no es una campaña completa:
no regenera canónicos ni se publica, así que no debe poder pasar por una.

**Transiciones** (`TRANSICIONES`): ``planned → running → computed → accredited``; cualquier estado
no terminal ``→ failed``. ``accredited`` y ``failed`` son terminales: un reintento es otra
complementaria con otro id. Una comprobación que falla al iniciar, cerrar o aceptar deja la
transacción en ``failed``; una transición ilegal sólo se rechaza. ★ La acreditación vive en la
LECTURA: `acreditar` re-verifica todo sin creer el ``status`` escrito.

**Código limpio, datos sellados.** `reports/` está versionado en git: `complement.json`, las
importaciones, la evidencia y los recibos lo ensucian por construcción. Por eso «código sucio» es todo
cambio FUERA de `reports/` (código, baraja, locks, `data/`); lo de dentro lo acreditan los hashes del
plan (panel, entradas congeladas, importaciones, recibos). Los recibos de la complementaria van a su
propio `LANES_DIR`: en `reports/campaign/e3/` hay recibos versionados de campañas anteriores.

**Código permitido, en TODO el ciclo** (`codigo_permitido`). HEAD = el sellado y nada sin commitear
fuera de `reports/`, comprobado al planificar, iniciar, cerrar, aceptar y verificar, en los productores
antes de calcular (`precondiciones_calculo`) y en quien firma: el recibo de cada lane
(`identidad_para_lanes`) y el sidecar de C3. Además, el código que CORRE tiene que salir de este
worktree (`codigo_cargado`): un script lanzado sin ``PYTHONPATH`` importa `vp_model` de la instalación
editable del venv, que es OTRO checkout. Auditoría de escrituras (M74-F, 9-oct): los lanes de E3 y
conformal sólo escriben bajo `reports/` y en rutas que `.gitignore` excluye (`mlruns_staging/`,
`__pycache__/`); no hay excepción fuera de `reports/`. Lo ignorado no cuenta como sucio, y la regla
que lo decide (`.gitignore`) es código: cambiarla sí ensucia.

**Entradas ignoradas por git, selladas aparte.** La DuckDB (`DUCKDB_REL`) es ignorada y no es
byte-reproducible: el plan sella su contenido LÓGICO —las filas de las tablas base y, aparte, el
catálogo SQL con las VISTAS que leen los consumidores— y exige que su `etl_run` la ligue al panel de la
madre; los productores comprueban además que la base que de verdad abren es ésa (`duckdb-ajena`).

**Evidencia de la madre.** Cada importación se acredita contra el miembro regular y ÚNICO de su nombre
en el tar de salidas de la madre (`EVIDENCIA_ARCHIVO`), con el tar y la copia de su `campaign.json`
listados en el manifiesto `SHA256SUMS` y esa copia igual a la madre sellada.

**Procedencia de C3.** Como C3 sólo puede mover ``baseline_coverage``, un baseline vacío o calculado
sobre otra cosa pasaría la comparación con la madre. Conformal sella un sidecar
(`PROCEDENCIA_REL`) con la importación que consumió cada tabla —baseline y pronósticos— y el sha de
sus salidas; el lector lo exige y exige un baseline finito en cada fila.

**C5 · CRPS de la receta campeona** (propósito ``champion_crps``, opción A del autor, 12-oct-2026). La
madre llevó a ``key_facts`` el CRPS de julio y `run_champion_crps` sólo sabe sellar dentro de una campaña
``running``. C5 no tiene lanes ni importaciones: la receta (`MANIFIESTO_CAMPEON_REL`) es una entrada
congelada, el productor corre con ``--complement`` y su recibo lleva la identidad de la complementaria y
las series omitidas con su motivo. El lector (`verificar_crps`) re-acredita recibo, identidad, código,
panel y protocolo completo, y recalcula la sustancia (`sustancia_crps`): puntuadas + omitidas = el
catálogo de la DuckDB sellada, filas coherentes y la fila ``ALL`` rehecha desde ellas.

**C6 · CRPS de referencia** (propósito ``baseline_crps``, 13-oct-2026). `crps_fad.csv` (ARIMA, SARIMA y DeepAR
sobre el hold-out FAD; el insumo del panel CRPS de F4) no tenía recibo ni lector. Mismo patrón que C5, sin
entradas congeladas: `run_crps_baseline --complement` sella el protocolo completo
(`champion.CRPS_BASE_PROTOCOLO`: tabla, bloque, modelos, muestras y semilla) y las omisiones con su motivo;
el lector (`verificar_crps_base`) re-acredita recibo, identidad y protocolo y recalcula la sustancia
(`sustancia_crps_base`): modelos del protocolo × catálogo de la DuckDB sellada, sin repetidas, CRPS finito y no
negativo, y las celdas vacías exactamente las omisiones declaradas.

``accredited`` NO autoriza propagar ni publicar: sólo dice que lo producido y lo importado son lo
que el plan dice. Plan de referencia: `Prompts/.../lote_M74F/PLAN_C4_C3_COMPLEMENTARIA_2026-10-08.md`.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import subprocess
import sys
import tarfile
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vp_model import artifact_receipt as ar
from vp_model.deck import Deck, LaneSpec

SCHEMA = "campaign-complement/1"
IMPORT_SCHEMA = "artifact-import/1"
RUTA_REL = Path("reports") / "campaign" / "complement.json"
CAMPANA_REL = Path("reports") / "campaign" / "campaign.json"
PANEL_REL = Path("data") / "processed" / "visa_panel_long.csv"
#: Recibos de la complementaria: NO `reports/campaign/e3/`, que trae 42 recibos versionados de otra corrida.
LANES_DIR = "reports/campaign/complement/e3"
#: Única raíz que la complementaria puede ensuciar; fuera de ella, todo cambio es código sucio.
DATOS_REL = "reports"
CONFORMAL_REL = "reports/eval/conformal_coverage.csv"
GAMMA_REL = "reports/eval/aci_gamma.json"
PROCEDENCIA_REL = "reports/eval/conformal_coverage.provenance.json"
PROCEDENCIA_SCHEMA = "conformal-provenance/1"
#: La DuckDB que consumen los lanes GBM y conformal (vía `vp_model.dataset`): ignorada por git.
DUCKDB_REL = "data/processed/visapredict.duckdb"
#: Junto al manifiesto `SHA256SUMS` de la evidencia de la madre: la copia de su transacción y el tar.
EVIDENCIA_IDENTIDAD = "campaign.json"
EVIDENCIA_ARCHIVO = "salidas_campana_completa.tar.gz"
#: Paquetes del producto: el código que corre tiene que salir del worktree sellado, no de otro checkout.
PAQUETES_PRODUCTO = ("vp_model", "vp_data", "pipeline")
#: Lo que conformal consume por tabla en C3: el deep-PI (baseline) y los pronósticos globales (ACI).
ROLES_C3 = ("baseline", "forecasts")


def artefacto_c3(rol: str, table: str) -> tuple[str, dict[str, Any]]:
    """``(ruta, protocolo)`` de lo que C3 importa por tabla: la ÚNICA fuente para quien declara y quien consume."""
    if rol == "baseline":
        return f"reports/eval/deep_pi_{table}.csv", {"table": table, "cqr": False}
    if rol == "forecasts":
        return f"reports/campaign/global_{table}_camp_diff_s1.csv", {"table": table, "variant": "camp_diff_s1"}
    raise ValueError(f"rol de C3 desconocido: {rol!r}")


#: C5 · CRPS de la receta campeona (opción A del autor, 12-oct-2026). Sin lanes ni importaciones: la receta
#: es una entrada congelada y el cálculo corre sobre el panel y la DuckDB sellados del worktree.
CRPS_REL = "reports/eval/crps_champion.csv"
MANIFIESTO_CAMPEON_REL = "reports/governance/champion_manifest.json"
#: C6 · CRPS de referencia de los modelos distribucionales (`run_crps_baseline.py`).
CRPS_BASE_REL = "reports/eval/crps_fad.csv"
#: El CSV que cada propósito de CRPS produce (y cuyo recibo previo impide iniciar).
SALIDA_CRPS = {"champion_crps": CRPS_REL, "baseline_crps": CRPS_BASE_REL}

#: Una madre `running` todavía produce y una `failed` no tiene identidad que prestar.
PADRE_ADMITIDO = frozenset({"computed", "validated"})
PROPOSITOS = frozenset({"e3", "deep_pi_conformal", "champion_crps", "baseline_crps"})
TRANSICIONES: dict[str, frozenset[str]] = {
    "planned": frozenset({"running", "failed"}),
    "running": frozenset({"computed", "failed"}),
    "computed": frozenset({"accredited", "failed"}),
    "accredited": frozenset(),
    "failed": frozenset(),
}
#: Columnas de `conformal_coverage.csv` que C3 puede cambiar; gamma y ACI deben quedar idénticos.
CONFORMAL_PERMITIDAS = frozenset({"baseline_coverage"})
_ID = re.compile(r"^compl_[A-Za-z0-9_.-]{1,120}$")
_PLAN = ("schema", "complement_id", "purpose", "parent", "code", "panel_sha256", "inputs", "lanes", "seed",
         "lanes_dir", "imports", "evidence", "duckdb")  # fmt: skip
_LINEA_SUMS = re.compile(r"^([0-9a-f]{64})  (\S(?:.*\S)?)$")

Problema = tuple[str, str]
Git = Callable[[Path], tuple[str, bool]]
AcreditarLanes = Callable[..., object]


class ComplementoError(RuntimeError):
    """La complementaria no se acredita. ``codigos`` permite a una prueba distinguir el motivo."""

    def __init__(self, problemas: Sequence[Problema]) -> None:
        self.problemas = list(problemas)
        self.codigos = {c for c, _ in self.problemas}
        super().__init__(f"{len(self.problemas)} problema(s):\n  " + "\n  ".join(f"[{c}] {m}" for c, m in problemas))


def huella(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _ahora() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def estado_git(root: Path) -> tuple[str, bool]:
    """``(HEAD, código sucio)`` del worktree. Fail-closed: sin git no hay código sellado.

    Sucio = cualquier cambio, versionado o no, FUERA de `DATOS_REL`. Dentro, la integridad la sellan
    los hashes del plan; exigir ahí un árbol limpio haría imposible siquiera planificar.
    """

    def git(*a: str) -> str:
        fin = subprocess.run(["git", *a], cwd=root, capture_output=True, text=True, check=False, timeout=60)
        if fin.returncode != 0:
            raise ComplementoError([("codigo", f"git {a[0]} falló en {root}: {fin.stderr.strip()[:160]}")])
        return fin.stdout

    fuera = git("status", "--porcelain", "--untracked-files=all", "--", ".", f":(exclude){DATOS_REL}")
    return git("rev-parse", "HEAD").strip(), bool(fuera.strip())


def codigo_permitido(root: Path, sellado: str | None, git: Git = estado_git) -> tuple[str, list[Problema]]:
    """LA comprobación de código: ``(HEAD vivo, problemas)``. HEAD sellado y nada sin commitear fuera de `reports/`.

    ``sellado=None`` sólo al planificar, cuando todavía no hay HEAD que comparar.
    """
    head, sucio = git(root)
    p: list[Problema] = []
    if sellado is not None and head != sellado:
        p.append(("codigo", f"el HEAD vivo {head[:12]} no es el código sellado {sellado[:12]}"))
    if sucio:
        p.append(("codigo-sucio", f"hay cambios sin commitear fuera de {DATOS_REL}/"))
    return head, p


def codigo_cargado(root: Path, modulos: Mapping[str, Any] | None = None) -> list[Problema]:
    """Los paquetes del producto ya importados en ESTE proceso salen del worktree ``root``.

    Un HEAD limpio no prueba nada si el proceso corre el `vp_model` de otro checkout (la instalación
    editable del venv apunta a la escena): el lane firmaría el código sellado con código ajeno.
    """
    cargados = sys.modules if modulos is None else modulos
    raiz = Path(root).resolve()
    p: list[Problema] = []
    for nombre in PAQUETES_PRODUCTO:
        modulo = cargados.get(nombre)
        if modulo is None:
            continue
        archivo = getattr(modulo, "__file__", None)
        if not archivo or not Path(archivo).resolve().is_relative_to(raiz):
            p.append(("codigo-ajeno", f"{nombre} se cargó de {archivo}, fuera del worktree sellado {raiz}"))
    return p


def _panel(root: Path) -> str | None:
    p = root / PANEL_REL
    return "sha256:" + ar.sha256_file(p) if p.is_file() else None


def leer_padre(parent_root: Path) -> dict[str, str]:
    """Identidad de la madre, leída de su `campaign.json` SIN escribir nada."""
    ruta = Path(parent_root) / CAMPANA_REL
    if not ruta.is_file():
        raise ComplementoError([("padre-ausente", f"no hay transacción madre en {ruta}")])
    estado = ar.loads_strict(ruta.read_text(encoding="utf-8"))
    if estado.get("status") not in PADRE_ADMITIDO:
        raise ComplementoError(
            [("padre-estado", f"la madre está en {estado.get('status')!r}; se exige {sorted(PADRE_ADMITIDO)}")]
        )
    faltan = [k for k in ("campaign_id", "source_git_sha", "panel_sha256", "input_seal_sha256") if not estado.get(k)]
    if faltan:
        raise ComplementoError([("padre-identidad", f"la madre no sella {faltan}")])
    return {
        "campaign_id": str(estado["campaign_id"]),
        "status": str(estado["status"]),
        "campaign_json_sha256": ar.sha256_file(ruta),
        "source_git_sha": str(estado["source_git_sha"]),
        "panel_sha256": str(estado["panel_sha256"]),
        "input_seal_sha256": str(estado["input_seal_sha256"]),
    }


def identidad(doc: Mapping[str, Any]) -> dict[str, str]:
    """La identidad que llevan los recibos de los lanes: la MISMA forma que la de una campaña."""
    return {
        "campaign_id": str(doc["complement_id"]),
        "source_git_sha": str(doc["code"]["source_git_sha"]),
        "panel_sha256": str(doc["panel_sha256"]),
    }


def plan_de_baraja(doc: Mapping[str, Any], deck: Deck) -> list[Problema]:
    """Los lanes y la semilla del plan sellado son EXACTAMENTE los que deriva la baraja del árbol."""
    plan = [{k: v for k, v in x.items() if k != "receipt_name"} for x in doc["lanes"]]
    p: list[Problema] = []
    if plan != [asdict(s) for s in deck.lanes()]:
        p.append(("plan-baraja", f"el plan sella {len(plan)} lanes y la baraja deriva otros {len(deck.lanes())}"))
    if doc["seed"] != deck.semilla():
        p.append(("plan-baraja", f"el plan sella la semilla {doc['seed']}, la baraja {deck.semilla()}"))
    return p


class _PlanLanes:
    """Baraja mínima construida DESDE el plan sellado: el lector no consulta otra lista."""

    def __init__(self, doc: Mapping[str, Any]) -> None:
        self._lanes = [LaneSpec(**{k: v for k, v in x.items() if k != "receipt_name"}) for x in doc["lanes"]]
        self._inputs = dict(doc["inputs"])

    def lanes(self) -> list[LaneSpec]:
        return list(self._lanes)

    def contrastar_entradas(self, root: Path) -> dict[str, dict]:
        """El acreditador E3 real contrasta las entradas congeladas del plan sellado."""
        return _contrastar_entradas(root, self._inputs)


def _contrastar_entradas(root: Path, inputs: Mapping[str, str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for rel, declared in sorted(inputs.items()):
        path = root / rel
        live = ar.sha256_file(path) if path.is_file() else None
        out[rel] = {
            "kind": "congelada",
            "declared": declared,
            "live": live,
            "status": "ausente" if live is None else ("igual" if live == declared else "difiere"),
        }
    return out


# ═══════════════════════════════ importaciones
def _verificar_importacion(
    root: Path, imp: Mapping[str, Any], doc: Mapping[str, Any], parent_root: Path
) -> list[Problema]:
    nombre = imp.get("artifact", "?")
    p: list[Problema] = []
    if imp.get("schema") != IMPORT_SCHEMA or imp.get("accreditation") != "importado":
        p.append(("importacion-esquema", f"{nombre}: no es una importación {IMPORT_SCHEMA} marcada 'importado'"))
    if imp.get("produced_by") != doc["parent"]["campaign_id"]:
        p.append(("importacion-otra-campana", f"{nombre}: producido por {imp.get('produced_by')!r}, no por la madre"))
    if imp.get("panel_sha256") != doc["panel_sha256"]:
        p.append(("importacion-panel", f"{nombre}: panel {str(imp.get('panel_sha256'))[:20]}… no es el de la madre"))
    destino = root / str(nombre)
    if not destino.is_file():
        p.append(("importacion-ausente", f"{nombre}: no está en el worktree complementario"))
    elif ar.sha256_file(destino) != imp.get("sha256"):
        p.append(("importacion-sustituida", f"{nombre}: el archivo no es el que se importó"))
    origen = Path(parent_root) / str(imp.get("origin_path", ""))
    if not origen.is_file() or ar.sha256_file(origen) != imp.get("sha256"):
        p.append(("importacion-origen", f"{nombre}: el origen en la madre falta o cambió"))
    if ar.receipt_path(destino).exists():
        p.append(("importacion-como-producida", f"{nombre}: lleva recibo de producción; una importación no lo tiene"))
    ev, archivo = imp.get("evidence") or {}, ((doc.get("evidence") or {}).get("archive") or {}).get("path")
    if not ev or ev.get("member") != nombre or ev.get("archive") != archivo:
        p.append(("importacion-sin-evidencia", f"{nombre}: no apunta a su miembro del tar de evidencia de la madre"))
    return p


def _declarar_importacion(
    decl: Mapping[str, Any], padre: Mapping[str, str], parent_root: Path, evidencia: Mapping[str, Any] | None
) -> dict:
    origen = Path(parent_root) / str(decl["origin_path"])
    if not origen.is_file():
        raise ComplementoError([("importacion-origen", f"{decl['origin_path']}: no existe en la madre")])
    return {
        "schema": IMPORT_SCHEMA,
        "artifact": str(decl["artifact"]),
        "origin_path": str(decl["origin_path"]),
        "sha256": ar.sha256_file(origen),
        "bytes": origen.stat().st_size,
        "produced_by": padre["campaign_id"],
        "panel_sha256": padre["panel_sha256"],
        "protocol": dict(decl["protocol"]),
        "consumers": list(decl.get("consumers") or []),
        "evidence": {"archive": evidencia["archive"]["path"], "member": str(decl["artifact"])} if evidencia else None,
        "accreditation": "importado",
    }


# ═══════════════════════════════ evidencia de la madre: manifiesto, identidad y tar
def _manifiesto(ruta: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for n, linea in enumerate(ruta.read_text(encoding="utf-8").splitlines(), 1):
        if not linea.strip():
            continue
        m = _LINEA_SUMS.match(linea)
        if m is None or m[2] in out:
            raise ComplementoError([("evidencia-manifiesto", f"{ruta.name}:{n}: línea ilegible o nombre repetido")])
        out[m[2]] = m[1]
    return out


def acreditar_evidencia(root: Path, manifiesto: str, padre: Mapping[str, str]) -> dict[str, dict[str, str]]:
    """La evidencia de la madre copiada bajo `reports/`: su manifiesto, su identidad y su tar, con hash.

    Identidad: la copia de `campaign.json` que lista el manifiesto es BYTE A BYTE la de la madre sellada
    (y dice su ``campaign_id``). Hash: el tar es el que lista el manifiesto. Fail-closed.
    """
    raiz = Path(root).resolve()
    ruta = (raiz / manifiesto).resolve()
    if not ruta.is_relative_to(raiz / DATOS_REL) or not ruta.is_file():
        raise ComplementoError([("evidencia-manifiesto", f"{manifiesto}: no existe bajo {DATOS_REL}/ del worktree")])
    sums = _manifiesto(ruta)
    out = {"manifest": {"path": str(ruta.relative_to(raiz)), "sha256": ar.sha256_file(ruta)}}
    p: list[Problema] = []
    for clave, nombre, codigo in (("identity", EVIDENCIA_IDENTIDAD, "evidencia-identidad"),
                                  ("archive", EVIDENCIA_ARCHIVO, "evidencia-archivo")):  # fmt: skip
        f = ruta.parent / nombre
        if nombre not in sums:
            p.append(("evidencia-manifiesto", f"{manifiesto}: no lista {nombre}"))
        elif not f.is_file() or ar.sha256_file(f) != sums[nombre]:
            p.append((codigo, f"{nombre}: falta o no es el que lista el manifiesto"))
        else:
            out[clave] = {"path": str(f.relative_to(raiz)), "sha256": sums[nombre]}
    if "identity" in out:
        copia = ar.loads_strict((raiz / out["identity"]["path"]).read_text(encoding="utf-8"))
        if (
            out["identity"]["sha256"] != padre["campaign_json_sha256"]
            or copia.get("campaign_id") != padre["campaign_id"]
        ):
            p.append(("evidencia-identidad", f"la evidencia no es de {padre['campaign_id']}: otro campaign.json"))
    if p:
        raise ComplementoError(p)
    return out


def miembros_tar(archivo: Path, nombres: Collection[str]) -> dict[str, list[tuple[bool, str | None]]]:
    """Por nombre: ``(regular, sha256)`` de CADA miembro del tar que se llama así. Lee bytes, no extrae."""
    buscados = set(nombres)
    out: dict[str, list[tuple[bool, str | None]]] = {n: [] for n in buscados}
    with tarfile.open(archivo, "r:*") as tar:
        for m in tar:
            nombre = m.name.removeprefix("./")
            if nombre not in buscados:
                continue
            fh = tar.extractfile(m) if m.isreg() else None
            sha = hashlib.sha256(fh.read()).hexdigest() if fh is not None else None
            out[nombre].append((m.isreg(), sha))
    return out


def _verificar_evidencia(root: Path, doc: Mapping[str, Any], solo: str | None = None) -> list[Problema]:
    """Re-lee la evidencia sellada y el miembro del tar de cada importación (o sólo de ``solo``)."""
    imports = [i for i in doc["imports"] if solo is None or i["artifact"] == solo]
    ev = doc.get("evidence")
    if not imports:
        return []
    if not ev:
        return [("importacion-sin-evidencia", "el plan importa artefactos sin evidencia de la madre")]
    p: list[Problema] = []
    for clave in ("manifest", "identity", "archive"):
        f = Path(root) / ev[clave]["path"]
        if not f.is_file() or ar.sha256_file(f) != ev[clave]["sha256"]:
            p.append(("evidencia-cambio", f"{ev[clave]['path']}: falta o cambió desde el plan"))
    if p:
        return p
    try:
        hallados = miembros_tar(Path(root) / ev["archive"]["path"], [i["artifact"] for i in imports])
    except (tarfile.TarError, OSError) as exc:
        return [("evidencia-archivo", f"{ev['archive']['path']}: tar ilegible: {exc}")]
    for imp in imports:
        nombre, h = imp["artifact"], hallados[imp["artifact"]]
        if not h:
            p.append(("importacion-tar-ausente", f"{nombre}: no es miembro del tar de la madre"))
        elif len(h) > 1:
            p.append(("importacion-tar-duplicado", f"{nombre}: {len(h)} miembros con ese nombre"))
        elif not h[0][0]:
            p.append(("importacion-tar-no-regular", f"{nombre}: el miembro del tar no es un archivo regular"))
        elif h[0][1] != imp["sha256"]:
            p.append(("importacion-tar-distinto", f"{nombre}: el miembro del tar no es el artefacto importado"))
    return p


# ═══════════════════════════════ DuckDB: ignorada por git, sellada por contenido
def sello_duckdb(ruta: Path) -> dict[str, Any]:
    """Huella LÓGICA de la DuckDB y el panel y estado con que se construyó, leídos de su `etl_run`.

    El `.duckdb` no es byte-reproducible ni lo versiona git: se sellan las filas (`huella_contenido`,
    la misma huella que el contrato de determinismo del almacén) y, como componente aparte, el catálogo
    SQL (`huella_catalogo`): los consumidores leen VISTAS, y una vista redefinida cambia lo que reciben
    con las mismas filas. Importa duckdb sólo aquí: los lanes deep (`ante_nf`, sin duckdb) importan este
    módulo y no consumen la base.
    """
    if not Path(ruta).is_file():
        raise ComplementoError([("duckdb-ausente", f"no hay DuckDB en {ruta}")])
    try:
        import duckdb

        from vp_data.duckdb_huella import huella_catalogo, huella_contenido
    except ImportError as exc:
        raise ComplementoError([("duckdb-ilegible", f"este intérprete no lee la DuckDB: {exc}")]) from exc
    try:
        con = duckdb.connect(str(ruta), read_only=True)
        try:
            filas = con.execute("SELECT panel_sha256, build_status FROM etl_run").fetchall()
        finally:
            con.close()
        contenido = huella_contenido(ruta)
        catalogo = huella_catalogo(ruta)
    except duckdb.Error as exc:
        raise ComplementoError([("duckdb-ilegible", f"{ruta}: {exc}")]) from exc
    if len(filas) != 1:
        raise ComplementoError([("duckdb-incompleta", f"etl_run con {len(filas)} filas: build interrumpido")])
    panel, estado = filas[0]
    return {
        "path": DUCKDB_REL,
        "content_sha256": contenido,
        "catalog_sha256": catalogo,
        "etl_panel_sha256": f"sha256:{panel}" if panel else None,
        "build_status": estado,
    }


def _problemas_duckdb(sello: Mapping[str, Any], panel: str) -> list[Problema]:
    p: list[Problema] = []
    if sello["etl_panel_sha256"] != panel:
        p.append(("duckdb-panel", f"la DuckDB se construyó con el panel {sello['etl_panel_sha256']}, no el sellado"))
    if sello["build_status"] != "ok":
        p.append(("duckdb-degradada", f"build_status {sello['build_status']!r}"))
    return p


def _verificar_duckdb(root: Path, doc: Mapping[str, Any], consumida: Path | None = None) -> list[Problema]:
    esperada = Path(root) / DUCKDB_REL
    p: list[Problema] = []
    if consumida is not None and Path(consumida).resolve() != esperada.resolve():
        p.append(("duckdb-ajena", f"el productor abriría {Path(consumida).resolve()}, no {esperada}"))
    try:
        vivo = sello_duckdb(esperada)
    except ComplementoError as exc:
        return p + exc.problemas
    if vivo["content_sha256"] != doc["duckdb"]["content_sha256"]:
        p.append(("duckdb-cambio", "el contenido de la DuckDB no es el sellado en el plan"))
    if vivo["catalog_sha256"] != doc["duckdb"].get("catalog_sha256"):
        p.append(("duckdb-catalogo", "las definiciones SQL (vistas, tablas) de la DuckDB no son las selladas"))
    return p + _problemas_duckdb(vivo, doc["panel_sha256"])


# ═══════════════════════════════ ciclo de vida
def _escribir(root: Path, doc: Mapping[str, Any]) -> None:
    texto = json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ar.replace_atomic(root / RUTA_REL, lambda t: t.write_text(texto, encoding="utf-8"))


def campana_concurrente(root: Path) -> list[Problema]:
    ruta = root / CAMPANA_REL
    if ruta.is_file() and ar.loads_strict(ruta.read_text(encoding="utf-8")).get("status") == "running":
        return [("campana-concurrente", "hay una campaña completa `running` en este worktree")]
    return []


def planificar(
    root: Path,
    *,
    parent_root: Path,
    complement_id: str,
    purpose: str,
    lanes: Sequence[LaneSpec],
    seed: int,
    frozen_inputs: Mapping[str, str],
    imports: Sequence[Mapping[str, Any]] = (),
    evidence: str | None = None,
    lanes_dir: str = LANES_DIR,
    git: Git = estado_git,
    ahora: Callable[[], str] = _ahora,
) -> dict:
    """Sella el plan (`planned`). No escribe nada si cualquier precondición falla.

    ``evidence``: el manifiesto `SHA256SUMS` de la evidencia de la madre, copiada bajo `reports/`.
    """
    root = Path(root)
    if (root / RUTA_REL).exists():
        raise ComplementoError([("ya-planificada", f"{RUTA_REL} ya existe: un reintento es otra complementaria")])
    padre = leer_padre(parent_root)
    p: list[Problema] = campana_concurrente(root)
    if not _ID.match(complement_id):
        p.append(("id", f"complement_id {complement_id!r} no cumple {_ID.pattern}"))
    if purpose not in PROPOSITOS:
        p.append(("proposito", f"{purpose!r} no es uno de {sorted(PROPOSITOS)}"))
    if purpose == "e3" and not lanes:
        p.append(("lanes-ausentes", "una complementaria de E3 sin lanes no acredita nada"))
    if purpose == "champion_crps" and MANIFIESTO_CAMPEON_REL not in frozen_inputs:
        p.append(
            ("entrada-congelada", f"un CRPS campeón sin {MANIFIESTO_CAMPEON_REL} sellado no dice de qué receta es")
        )
    nombres = [s.receipt_name for s in lanes]
    if len(set(nombres)) != len(nombres):
        p.append(("lane-duplicado", "el plan repite lanes"))
    p += [("semilla", f"{s.receipt_name}: semilla {s.seed}, el plan fija {seed}") for s in lanes if s.seed != seed]
    if _panel(root) != padre["panel_sha256"]:
        p.append(("panel", "el panel en disco no es el que selló la madre"))
    head, problemas_codigo = codigo_permitido(root, None, git)
    p += problemas_codigo
    for rel, sha in sorted(frozen_inputs.items()):
        f = root / rel
        if not f.is_file() or ar.sha256_file(f) != sha:
            p.append(("entrada-congelada", f"{rel}: falta o no es la que declara la baraja"))
    duck: dict[str, Any] | None = None
    try:
        duck = sello_duckdb(root / DUCKDB_REL)
        p += _problemas_duckdb(duck, padre["panel_sha256"])
    except ComplementoError as exc:
        p += exc.problemas
    evidencia = None
    if imports and evidence is None:
        p.append(("importacion-sin-evidencia", "el plan importa artefactos sin evidencia de la madre"))
    elif evidence is not None:
        try:
            evidencia = acreditar_evidencia(root, evidence, padre)
        except ComplementoError as exc:
            p += exc.problemas
    if p:
        raise ComplementoError(p)
    importadas = [_declarar_importacion(d, padre, parent_root, evidencia) for d in imports]
    doc: dict[str, Any] = {
        "schema": SCHEMA,
        "complement_id": complement_id,
        "purpose": purpose,
        "parent": padre,
        "code": {"source_git_sha": head},
        "panel_sha256": padre["panel_sha256"],
        "inputs": dict(sorted(frozen_inputs.items())),
        "lanes": [{**asdict(s), "receipt_name": s.receipt_name} for s in lanes],
        "seed": seed,
        "lanes_dir": lanes_dir,
        "imports": importadas,
        "evidence": evidencia,
        "duckdb": duck,
    }
    p = [x for imp in importadas for x in _verificar_importacion(root, imp, doc, parent_root)]
    p += _verificar_evidencia(root, doc)
    if p:
        raise ComplementoError(p)
    doc.update(
        plan_sha256=huella({k: doc[k] for k in _PLAN}),
        status="planned",
        revision=1,
        timestamps={"planned_at": ahora()},
        failure=None,
        outcome=None,
    )
    _escribir(root, doc)
    return doc


def leer(root: Path) -> dict:
    """El documento, con el plan re-verificado contra su huella."""
    ruta = Path(root) / RUTA_REL
    if not ruta.is_file():
        raise ComplementoError([("ausente", f"no hay complementaria en {ruta}")])
    doc = ar.loads_strict(ruta.read_text(encoding="utf-8"))
    if doc.get("schema") != SCHEMA:
        raise ComplementoError([("esquema", f"esquema {doc.get('schema')!r}")])
    if any(k not in doc for k in _PLAN) or huella({k: doc[k] for k in _PLAN}) != doc.get("plan_sha256"):
        raise ComplementoError([("plan-editado", "el plan no coincide con la huella sellada al planificar")])
    if doc.get("status") not in TRANSICIONES:
        raise ComplementoError([("estado", f"estado desconocido {doc.get('status')!r}")])
    return doc


def _mover(root: Path, doc: dict, nuevo: str, ahora: Callable[[], str], **campos: Any) -> dict:
    if nuevo not in TRANSICIONES[doc["status"]]:
        raise ComplementoError([("transicion", f"{doc['status']} → {nuevo} no es una transición admitida")])
    doc = {**doc, **campos, "status": nuevo, "revision": int(doc["revision"]) + 1}
    doc["timestamps"] = {**doc["timestamps"], f"{nuevo}_at": ahora()}
    _escribir(root, doc)
    return doc


def _sellado_local(
    root: Path, doc: Mapping[str, Any], git: Git, duckdb_consumida: Path | None = None
) -> list[Problema]:
    """Lo que el worktree tiene que seguir siendo: código permitido, panel, entradas congeladas y DuckDB."""
    p = codigo_permitido(root, doc["code"]["source_git_sha"], git)[1]
    if _panel(root) != doc["panel_sha256"]:
        p.append(("panel", "el panel en disco no es el sellado"))
    p += [
        ("entrada-congelada", f"{rel}: {contrast['status']} frente al plan sellado")
        for rel, contrast in _contrastar_entradas(root, doc["inputs"]).items()
        if contrast["status"] != "igual"
    ]
    return p + _verificar_duckdb(root, doc, duckdb_consumida)


def _comun(root: Path, doc: Mapping[str, Any], parent_root: Path, git: Git) -> list[Problema]:
    p: list[Problema] = []
    try:
        padre = leer_padre(parent_root)
        if padre["campaign_json_sha256"] != doc["parent"]["campaign_json_sha256"]:
            p.append(("padre-cambio", "el campaign.json de la madre cambió desde el plan"))
    except ComplementoError as exc:
        p += exc.problemas
    p += _sellado_local(root, doc, git)
    p += [x for imp in doc["imports"] for x in _verificar_importacion(root, imp, doc, parent_root)]
    return p + _verificar_evidencia(root, doc)


def _o_fallar(root: Path, doc: dict, etapa: str, p: list[Problema], ahora: Callable[[], str]) -> None:
    if p:
        _mover(root, doc, "failed", ahora, failure={"stage": etapa, "codes": sorted({c for c, _ in p})})
        raise ComplementoError(p)


def iniciar(root: Path, *, parent_root: Path, git: Git = estado_git, ahora: Callable[[], str] = _ahora) -> dict:
    """``planned → running``: sin salidas previas, sin campaña concurrente, mismo código, panel y madre."""
    root = Path(root)
    doc = leer(root)
    if "running" not in TRANSICIONES[doc["status"]]:
        raise ComplementoError([("transicion", f"{doc['status']} → running no es una transición admitida")])
    p = _comun(root, doc, parent_root, git) + campana_concurrente(root)
    previas = sorted((root / doc["lanes_dir"]).glob("receipt_*.json"))
    if previas:
        p.append(("salidas-previas", f"{len(previas)} recibo(s) de lane anteriores al inicio"))
    salida = SALIDA_CRPS.get(doc["purpose"])
    if salida is not None and ar.receipt_path(root / salida).exists():
        p.append(("salidas-previas", f"{salida} ya tiene recibo antes del inicio"))
    _o_fallar(root, doc, "iniciar", p, ahora)
    return _mover(root, doc, "running", ahora)


def identidad_en_curso(root: Path) -> dict[str, str]:
    """Para los productores: la identidad que sellan los lanes, sólo con la complementaria `running`."""
    doc = leer(root)
    if doc["status"] != "running":
        raise ComplementoError([("no-en-curso", f"la complementaria está en {doc['status']!r}")])
    return identidad(doc)


def identidad_para_lanes(root: Path, git: Git = estado_git) -> dict[str, str] | None:
    """Para el recibo de un lane: la identidad de la complementaria `running` de E3, o ``None``.

    ``None`` si no hay complementaria o si no está en curso (el lector rechazará ese recibo). Un plan
    ilegible o editado, una complementaria de otro propósito en curso o un código que no es el
    permitido (HEAD, árbol sucio o paquetes cargados de otro checkout) detienen al lane: firmar el
    código sellado con otro código es lo que esta identidad existe para impedir.
    """
    if not (Path(root) / RUTA_REL).is_file():
        return None
    doc = leer(Path(root))
    if doc["status"] != "running":
        return None
    if doc["purpose"] != "e3":
        raise ComplementoError([("proposito", f"la complementaria en curso es de {doc['purpose']!r}, no de E3")])
    p = codigo_permitido(Path(root), doc["code"]["source_git_sha"], git)[1] + codigo_cargado(Path(root))
    if p:
        raise ComplementoError(p)
    return identidad(doc)


def precondiciones_calculo(root: Path, *, proposito: str, duckdb_consumida: Path, git: Git = estado_git) -> dict:
    """Lo que un PRODUCTOR exige antes de calcular; devuelve el plan. Fail-closed.

    Complementaria `running` de su propósito y sin campaña concurrente; código permitido y cargado de
    este worktree; panel, entradas congeladas y DuckDB sellados; y la DuckDB que de verdad va a abrir
    (``duckdb_consumida``, resuelta como la resuelve él) es la del worktree.
    """
    root = Path(root)
    doc = leer(root)
    p = campana_concurrente(root)
    if doc["status"] != "running":
        p.append(("no-en-curso", f"la complementaria está en {doc['status']!r}"))
    if doc["purpose"] != proposito:
        p.append(("proposito", f"la complementaria es de {doc['purpose']!r}, no de {proposito!r}"))
    p += _sellado_local(root, doc, git, duckdb_consumida) + codigo_cargado(root)
    if p:
        raise ComplementoError(p)
    return doc


def _verificar_todo(root: Path, doc: Mapping[str, Any], parent_root: Path, acreditar_lanes: AcreditarLanes, git: Git):
    p = _comun(root, doc, parent_root, git)
    if doc["lanes"]:
        try:
            acreditar_lanes(root / doc["lanes_dir"], _PlanLanes(doc), identidad(doc), root=root)
        except (RuntimeError, ValueError, OSError) as exc:
            p.append(("lanes", str(exc)))
    if doc["purpose"] == "deep_pi_conformal":
        try:
            p += aceptacion_c3(parent_root / "reports/eval", root / "reports/eval")
        except ComplementoError as exc:
            p += exc.problemas
        except (OSError, ValueError, KeyError) as exc:
            p.append(("conformal-resultados", f"no se pueden acreditar los resultados de C3: {exc}"))
        p += _verificar_procedencia_c3(root, doc)
    if doc["purpose"] == "champion_crps":
        p += verificar_crps(root, doc)
    if doc["purpose"] == "baseline_crps":
        p += verificar_crps_base(root, doc)
    return p


def cerrar_computo(
    root: Path,
    *,
    parent_root: Path,
    acreditar_lanes: AcreditarLanes,
    git: Git = estado_git,
    ahora: Callable[[], str] = _ahora,
) -> dict:
    """``running → computed``: los lanes exactos del plan, con su identidad, semilla y hashes."""
    root = Path(root)
    doc = leer(root)
    if "computed" not in TRANSICIONES[doc["status"]]:
        raise ComplementoError([("transicion", f"{doc['status']} → computed no es una transición admitida")])
    _o_fallar(root, doc, "cerrar_computo", _verificar_todo(root, doc, parent_root, acreditar_lanes, git), ahora)
    return _mover(root, doc, "computed", ahora, outcome={"lanes": len(doc["lanes"]), "imports": len(doc["imports"])})


def acreditar(root: Path, *, parent_root: Path, acreditar_lanes: AcreditarLanes, git: Git = estado_git) -> dict:
    """LECTOR: re-verifica todo sin creer el estado escrito. No escribe."""
    root = Path(root)
    doc = leer(root)
    if doc["status"] not in {"computed", "accredited"}:
        raise ComplementoError([("estado", f"una complementaria en {doc['status']!r} no se consume")])
    p = _verificar_todo(root, doc, parent_root, acreditar_lanes, git)
    if p:
        raise ComplementoError(p)
    return {"identity": identidad(doc), "parent": doc["parent"]["campaign_id"], "lanes": len(doc["lanes"]),
            "imports": [i["artifact"] for i in doc["imports"]]}  # fmt: skip


def aceptar(
    root: Path,
    *,
    parent_root: Path,
    acreditar_lanes: AcreditarLanes,
    git: Git = estado_git,
    ahora: Callable[[], str] = _ahora,
) -> dict:
    """``computed → accredited`` si el lector la acredita. NO autoriza propagar ni publicar."""
    root = Path(root)
    doc = leer(root)
    if "accredited" not in TRANSICIONES[doc["status"]]:
        raise ComplementoError([("transicion", f"{doc['status']} → accredited no es una transición admitida")])
    _o_fallar(root, doc, "aceptar", _verificar_todo(root, doc, parent_root, acreditar_lanes, git), ahora)
    return _mover(root, doc, "accredited", ahora)


def fallar(root: Path, motivo: str, *, ahora: Callable[[], str] = _ahora) -> dict:
    """Aborto explícito (p. ej. un lane muere). Terminal."""
    doc = leer(Path(root))
    return _mover(Path(root), doc, "failed", ahora, failure={"stage": "abortada", "codes": [], "reason": motivo})


def importacion_acreditada(root: Path, artifact: str, protocol: Mapping[str, Any], *, parent_root: Path) -> dict:
    """Para el CONSUMIDOR (conformal en C3): el artefacto importado, con su origen declarado.

    Devuelve ``source = "importado:<madre>"``: quien lo use debe rotularlo así; jamás como producido
    por la complementaria.
    """
    root = Path(root)
    doc = leer(root)
    if doc["status"] not in {"running", "computed", "accredited"}:
        raise ComplementoError([("estado", f"una complementaria en {doc['status']!r} no presta importaciones")])
    imp = next((i for i in doc["imports"] if i["artifact"] == artifact), None)
    if imp is None:
        raise ComplementoError([("importacion-ausente", f"{artifact} no está declarado en el plan")])
    p = _verificar_importacion(root, imp, doc, parent_root) + _verificar_evidencia(root, doc, solo=artifact)
    if imp["protocol"] != dict(protocol):
        p.append(("importacion-protocolo", f"{artifact}: protocolo {imp['protocol']} ≠ {dict(protocol)}"))
    if p:
        raise ComplementoError(p)
    return {"source": f"importado:{doc['parent']['campaign_id']}", "sha256": imp["sha256"], "artifact": artifact}


# ═══════════════════════════════ aceptación de C3
def diferencias_conformal(madre: Path, complemento: Path, *, clave: str = "table") -> dict[str, list[str]]:
    """Columnas que cambian, por fila, entre el `conformal_coverage.csv` de la madre y el de C3."""

    def filas(p: Path) -> dict[str, dict[str, str]]:
        with Path(p).open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            columns = reader.fieldnames or []
            if clave not in columns or len(columns) != len(set(columns)):
                raise ComplementoError([("conformal-filas", f"{p}: encabezado ausente o duplicado")])
            out: dict[str, dict[str, str]] = {}
            for row in reader:
                if None in row or any(value is None for value in row.values()) or not row[clave]:
                    raise ComplementoError([("conformal-filas", f"{p}: fila incompleta o fuera del encabezado")])
                if row[clave] in out:
                    raise ComplementoError([("conformal-filas", f"{p}: fila duplicada {row[clave]!r}")])
                out[row[clave]] = row
            if not out:
                raise ComplementoError([("conformal-filas", f"{p}: no hay filas")])
            return out

    a, b = filas(madre), filas(complemento)
    if set(a) != set(b):
        raise ComplementoError([("conformal-filas", f"filas {sorted(a)} ≠ {sorted(b)}")])
    return {k: sorted(c for c in set(a[k]) | set(b[k]) if a[k].get(c) != b[k].get(c)) for k in sorted(a)}


def aceptacion_c3(madre_eval: Path, compl_eval: Path) -> list[Problema]:
    """C3 sólo puede mover la comparación con el baseline: gamma y ACI deben salir byte-idénticos."""
    p: list[Problema] = []
    for k, cols in diferencias_conformal(
        madre_eval / "conformal_coverage.csv", compl_eval / "conformal_coverage.csv"
    ).items():
        fuera = sorted(set(cols) - CONFORMAL_PERMITIDAS)
        if fuera:
            p.append(("conformal-fuera-de-alcance", f"{k}: cambian {fuera}"))
    if ar.sha256_file(madre_eval / "aci_gamma.json") != ar.sha256_file(compl_eval / "aci_gamma.json"):
        p.append(("gamma", "aci_gamma.json cambió: C3 no debía tocar la selección de gamma"))
    return p


def sellar_procedencia_c3(
    root: Path, consumidas: Mapping[str, Mapping[str, Mapping[str, str]]], git: Git = estado_git
) -> dict:
    """Sidecar de C3: qué importación consumió conformal por tabla y rol, y el sha de lo que escribió.

    ``consumidas`` = ``{tabla: {rol: importacion_acreditada(...)}}``. Lo escribe conformal DESPUÉS de
    sus salidas; sólo con la complementaria `running` y con el código permitido (es una firma, como
    el recibo de un lane). No cambia el esquema del CSV que leen F4 y `key_facts`.
    """
    root = Path(root)
    doc = leer(root)
    if doc["status"] != "running":
        raise ComplementoError([("no-en-curso", f"la complementaria está en {doc['status']!r}")])
    p = codigo_permitido(root, doc["code"]["source_git_sha"], git)[1] + codigo_cargado(root)
    if p:
        raise ComplementoError(p)
    sidecar = {
        "schema": PROCEDENCIA_SCHEMA,
        "complement_id": doc["complement_id"],
        "parent": doc["parent"]["campaign_id"],
        "tables": {t: {rol: dict(consumidas[t][rol]) for rol in ROLES_C3} for t in sorted(consumidas)},
        "outputs": {rel: ar.sha256_file(root / rel) for rel in (CONFORMAL_REL, GAMMA_REL)},
    }
    texto = json.dumps(sidecar, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ar.replace_atomic(root / PROCEDENCIA_REL, lambda t: t.write_text(texto, encoding="utf-8"))
    return sidecar


def _verificar_procedencia_c3(root: Path, doc: Mapping[str, Any]) -> list[Problema]:
    """El sidecar acredita que cada tabla consumió importaciones DE LA MADRE y que el baseline existe."""
    ruta = root / PROCEDENCIA_REL
    try:
        sc = ar.loads_strict(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [("conformal-procedencia", f"sin procedencia legible de C3 ({PROCEDENCIA_REL}): {exc}")]
    madre = doc["parent"]["campaign_id"]
    p: list[Problema] = []
    if (sc.get("schema"), sc.get("complement_id"), sc.get("parent")) != (
        PROCEDENCIA_SCHEMA,
        doc["complement_id"],
        madre,
    ):
        p.append(("conformal-procedencia", "el sidecar no es de esta complementaria ni de su madre"))
    salidas = sc.get("outputs") or {}
    for rel in (CONFORMAL_REL, GAMMA_REL):
        f = root / rel
        if not f.is_file() or salidas.get(rel) != ar.sha256_file(f):
            p.append(("conformal-procedencia", f"{rel} no es la salida que selló conformal"))
    importadas = {i["artifact"]: i["sha256"] for i in doc["imports"]}
    tablas = sc.get("tables") or {}
    for t in sorted(tablas):
        for rol in ROLES_C3:
            c = (tablas[t] or {}).get(rol) or {}
            if c.get("source") != f"importado:{madre}" or importadas.get(c.get("artifact")) != c.get("sha256"):
                p.append(("conformal-procedencia", f"{t}/{rol}: no consumió una importación declarada de la madre"))
    try:
        with (root / CONFORMAL_REL).open(newline="", encoding="utf-8") as fh:
            filas = list(csv.DictReader(fh))
    except OSError:
        return p
    if sorted(r.get("table") or "" for r in filas) != sorted(tablas):
        p.append(("conformal-procedencia", f"el sidecar cubre {sorted(tablas)} y el CSV otras tablas"))
    for r in filas:
        try:
            base = float(r.get("baseline_coverage") or "nan")
        except ValueError:
            base = math.nan
        if not (math.isfinite(base) and 0.0 <= base <= 1.0):
            p.append(
                ("conformal-baseline-vacio", f"{r.get('table')}: baseline_coverage {r.get('baseline_coverage')!r}")
            )
    return p


# ═══════════════════════════════ C5 · CRPS de la receta campeona
def identidad_crps(
    root: Path, *, duckdb_consumida: Path, git: Git = estado_git, proposito: str = "champion_crps"
) -> dict[str, str]:
    """Para `run_champion_crps --complement` al SELLAR: la complementaria `champion_crps` sigue `running`
    y el worktree sigue siendo el sellado. Mismas precondiciones que antes de calcular: un commit o una
    DuckDB cambiados a mitad de cálculo dejan el CSV sin firma."""
    return identidad(precondiciones_calculo(root, proposito=proposito, duckdb_consumida=duckdb_consumida, git=git))


def verificar_crps(root: Path, doc: Mapping[str, Any]) -> list[Problema]:
    """LECTOR de C5: el recibo del CSV es de ESTA complementaria, con su código, su panel y el protocolo
    completo; y la sustancia del CSV se recalcula en vez de creer al escritor (`sustancia_crps`)."""
    from vp_model import champion

    root = Path(root)
    csv_p = root / CRPS_REL
    if not csv_p.is_file():
        return [("crps-resultados", f"no hay {CRPS_REL}: el CRPS campeón no se calculó")]
    try:
        acta = ar.loads_strict(ar.receipt_path(csv_p).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [("crps-recibo", f"sin recibo legible de {CRPS_REL}: {exc}")]
    p: list[Problema] = []
    if acta.get("schema") != champion.CRPS_RECEIPT_SCHEMA:
        p.append(("crps-recibo", f"recibo con esquema {acta.get('schema')!r}"))
    if acta.get("artifact_sha256") != ar.sha256_file(csv_p):
        p.append(("crps-recibo", "el CSV cambió desde que se selló su recibo"))
    ident = identidad(doc)
    if (acta.get("campaign_id"), acta.get("code_sha"), acta.get("panel_sha256")) != (
        ident["campaign_id"],
        ident["source_git_sha"],
        ident["panel_sha256"],
    ):
        p.append(("crps-identidad", f"el recibo es de {acta.get('campaign_id')!r}, no de esta complementaria"))
    return p + validar_crps(root, acta)


def _cobertura(acta: Mapping[str, Any]) -> tuple[dict, list[Problema]]:
    """La cobertura del recibo como objeto; un recibo mal tipado es un problema, nunca una excepción."""
    cobertura = acta.get("coverage")
    if not isinstance(cobertura, dict):
        return {}, [("crps-recibo", f"la cobertura del recibo no es un objeto: {cobertura!r:.80}")]
    return cobertura, []


def _catalogo(root: Path, table: str, block: str) -> tuple[set[tuple[str, str]] | None, list[Problema]]:
    """``(país, categoría)`` del catálogo de la DuckDB sellada; sin DuckDB legible, un problema y ``None``."""
    import duckdb

    from vp_model import dataset

    try:
        cat = dataset.list_series(table=table, block=block, db_path=Path(root) / DUCKDB_REL)
    except (OSError, RuntimeError, duckdb.Error) as exc:
        return None, [("crps-cobertura", f"sin catálogo legible contra el que medir la cobertura: {exc}")]
    return {(str(r.country), str(r.category)) for r in cat.itertuples()}, []


def validar_crps(root: Path, acta: Mapping[str, Any]) -> list[Problema]:
    """Protocolo, forma y sustancia del recibo del CRPS campeón (sin identidad): lo comparten el lector de la
    complementaria y el consumidor cuando lo respalda una campaña (auditoría C6: antes sólo miraba identidad)."""
    from vp_model import champion

    p: list[Problema] = []
    if acta.get("protocol") != champion.CRPS_PROTOCOLO:
        p.append(
            ("crps-protocolo", f"protocolo {acta.get('protocol')} ≠ la corrida completa {champion.CRPS_PROTOCOLO}")
        )
    cobertura, forma = _cobertura(acta)
    if forma:
        return p + forma
    if cobertura.get("tables") != sorted(champion.CRPS_PROTOCOLO["tables"]):
        p.append(("crps-protocolo", f"el recibo cubre las tablas {cobertura.get('tables')}"))
    return p + sustancia_crps(Path(root), Path(root) / CRPS_REL, cobertura.get("skipped"))


def _num(fila: Mapping[str, str], campo: str) -> float:
    valor = float(fila.get(campo) or "nan")
    if not math.isfinite(valor):
        raise ValueError(f"{campo}={fila.get(campo)!r}")
    return valor


def _entero(fila: Mapping[str, str], campo: str) -> int:
    """Un conteo: finito y de valor ENTERO (``25.0`` vale, ``24.5`` no). #67: ``int()`` truncaba."""
    valor = _num(fila, campo)
    if not valor.is_integer():
        raise ValueError(f"{campo}={fila.get(campo)!r} no es un conteo entero")
    return int(valor)


def sustancia_crps(root: Path, csv_p: Path, omitidas: Any) -> list[Problema]:
    """La sustancia del CSV, recalculada al leer.

    * Series puntuadas + omitidas declaradas (con motivo) = EXACTAMENTE el catálogo que la DuckDB sellada
      da para cada tabla del protocolo: una fila no puede valer por el universo entero ni una serie
      perderse en silencio.
    * Cada fila: CRPS finito y no negativo, `k95 ≤ n` y `cov95 = k95/n`.
    * La fila ``ALL`` de cada tabla se rehace desde sus filas (n, k, cobertura y CRPS medio) y su receta
      es la del manifiesto congelado.
    """
    from vp_model import champion

    protocolo = champion.CRPS_PROTOCOLO
    tablas = list(protocolo["tables"])
    p: list[Problema] = []
    try:
        with Path(csv_p).open(newline="", encoding="utf-8") as fh:
            filas = list(csv.DictReader(fh))
        manifiesto = ar.loads_strict((Path(root) / MANIFIESTO_CAMPEON_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [("crps-sustancia", f"no se puede leer el CSV o el manifiesto: {exc}")]
    if not isinstance(omitidas, list) or any(
        not isinstance(o, dict) or set(o) != {"table", "country", "category", "reason"} or not str(o["reason"]).strip()
        for o in omitidas
    ):
        return [("crps-cobertura", "el recibo no declara las series omitidas (tabla, país, categoría y motivo)")]
    for fila in filas:
        if fila.get("table") not in tablas:
            p.append(("crps-sustancia", f"fila de una tabla fuera del protocolo: {fila.get('table')!r}"))
    for tabla in tablas:
        series = [f for f in filas if f.get("table") == tabla and f.get("country") != "ALL"]
        totales = [f for f in filas if f.get("table") == tabla and f.get("country") == "ALL"]
        claves = [(f.get("country"), f.get("category")) for f in series]
        if len(set(claves)) != len(claves):
            p.append(("crps-sustancia", f"{tabla}: series repetidas en el CSV"))
        saltadas = {(o["country"], o["category"]) for o in omitidas if o["table"] == tabla}
        esperado, sin_catalogo = _catalogo(root, tabla, str(protocolo["block"]))
        if esperado is None:
            p += sin_catalogo
            continue
        if set(claves) & saltadas:
            p.append(("crps-cobertura", f"{tabla}: series a la vez puntuadas y omitidas"))
        if set(claves) | saltadas != esperado:
            faltan, sobran = esperado - set(claves) - saltadas, (set(claves) | saltadas) - esperado
            p.append(("crps-cobertura", f"{tabla}: faltan {sorted(faltan)[:5]} · sobran {sorted(sobran)[:5]}"))
        if len(totales) != 1 or not series:
            p.append(("crps-sustancia", f"{tabla}: {len(totales)} filas ALL y {len(series)} series puntuadas"))
            continue
        try:
            crps = [_num(f, "crps") for f in series]
            n = [_entero(f, "n_holdout_F") for f in series]
            k = [_entero(f, "k95") for f in series]
            for f, c, ni, ki in zip(series, crps, n, k, strict=True):
                if c < 0 or ni <= 0 or not 0 <= ki <= ni or _num(f, "cov95") != round(ki / ni, 4):
                    p.append(("crps-sustancia", f"{tabla}/{f.get('country')}/{f.get('category')}: fila incoherente"))
            tot = totales[0]
            rehecho = {
                "n_series": len(series),
                "n_holdout_F": sum(n),
                "k95": sum(k),
                "cov95": round(sum(k) / sum(n), 4),
                "crps": round(sum(crps) / len(crps), 2),
            }
            leido = {c: _num(tot, c) for c in rehecho}  # un ALL fraccionario no iguala sumas enteras
        except ValueError as exc:
            p.append(("crps-sustancia", f"{tabla}: valor no finito, no entero o ilegible ({exc})"))
            continue
        if leido != {c: float(v) for c, v in rehecho.items()}:
            p.append(("crps-sustancia", f"{tabla}: la fila ALL {leido} no es la que dan sus filas {rehecho}"))
        receta = "+".join((manifiesto.get(tabla) or {}).get("models") or [])
        if tot.get("recipe") != receta:
            p.append(("crps-sustancia", f"{tabla}: receta {tot.get('recipe')!r} ≠ la del manifiesto {receta!r}"))
    return p


# ═══════════════════════════════ C6 · CRPS de referencia
def verificar_crps_base(root: Path, doc: Mapping[str, Any]) -> list[Problema]:
    """LECTOR de C6: el recibo de `crps_fad.csv` es de ESTA complementaria, con su código, su panel y el
    protocolo completo (semilla y muestras incluidas); la sustancia se recalcula (`sustancia_crps_base`)."""
    from vp_model import champion

    root = Path(root)
    csv_p = root / CRPS_BASE_REL
    if not csv_p.is_file():
        return [("crps-resultados", f"no hay {CRPS_BASE_REL}: el CRPS de referencia no se calculó")]
    try:
        acta = ar.loads_strict(ar.receipt_path(csv_p).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [("crps-recibo", f"sin recibo legible de {CRPS_BASE_REL}: {exc}")]
    p: list[Problema] = []
    if acta.get("schema") != champion.CRPS_BASE_RECEIPT_SCHEMA:
        p.append(("crps-recibo", f"recibo con esquema {acta.get('schema')!r}"))
    if acta.get("artifact_sha256") != ar.sha256_file(csv_p):
        p.append(("crps-recibo", "el CSV cambió desde que se selló su recibo"))
    ident = identidad(doc)
    if (acta.get("campaign_id"), acta.get("code_sha"), acta.get("panel_sha256")) != (
        ident["campaign_id"],
        ident["source_git_sha"],
        ident["panel_sha256"],
    ):
        p.append(("crps-identidad", f"el recibo es de {acta.get('campaign_id')!r}, no de esta complementaria"))
    return p + validar_crps_base(root, acta)


def validar_crps_base(root: Path, acta: Mapping[str, Any]) -> list[Problema]:
    """Protocolo, forma y sustancia del recibo de `crps_fad.csv` (sin identidad); ver :func:`validar_crps`."""
    from vp_model import champion

    protocolo = champion.CRPS_BASE_PROTOCOLO
    p: list[Problema] = []
    if acta.get("protocol") != protocolo:
        p.append(("crps-protocolo", f"protocolo {acta.get('protocol')} ≠ la corrida completa {protocolo}"))
    cobertura, forma = _cobertura(acta)
    if forma:
        return p + forma
    if cobertura.get("table") != protocolo["table"]:
        p.append(("crps-protocolo", f"el recibo cubre la tabla {cobertura.get('table')!r}"))
    return p + sustancia_crps_base(Path(root), Path(root) / CRPS_BASE_REL, cobertura.get("skipped"))


def sustancia_crps_base(root: Path, csv_p: Path, omitidas: Any) -> list[Problema]:
    """La sustancia de `crps_fad.csv`, recalculada al leer.

    * Filas = EXACTAMENTE modelos del protocolo × catálogo que la DuckDB sellada da para la tabla y el bloque,
      sin repetidas: un modelo o una serie no pueden faltar ni sobrar en silencio.
    * Cada celda: CRPS finito y no negativo, o vacía. Las vacías son EXACTAMENTE las omisiones que el recibo
      declara con motivo (`run_crps_baseline` escribe NaN cuando una serie falla).
    """
    from vp_model import champion

    protocolo = champion.CRPS_BASE_PROTOCOLO
    try:
        with Path(csv_p).open(newline="", encoding="utf-8") as fh:
            filas = list(csv.DictReader(fh))
    except OSError as exc:
        return [("crps-sustancia", f"no se puede leer el CSV: {exc}")]
    campos = ("model", "country", "category")
    if not isinstance(omitidas, list) or any(
        not isinstance(o, dict) or set(o) != {*campos, "reason"} or not str(o["reason"]).strip() for o in omitidas
    ):
        return [("crps-cobertura", "el recibo no declara las omisiones (modelo, país, categoría y motivo)")]
    claves = [tuple(f.get(c) for c in campos) for f in filas]
    series, sin_catalogo = _catalogo(root, str(protocolo["table"]), str(protocolo["block"]))
    if series is None:
        return sin_catalogo
    esperado = {(m, pais, cat) for m in protocolo["models"] for pais, cat in series}
    p: list[Problema] = []
    if len(set(claves)) != len(claves):
        p.append(("crps-cobertura", "filas repetidas en el CSV"))
    if set(claves) != esperado:
        faltan, sobran = esperado - set(claves), set(claves) - esperado
        p.append(("crps-cobertura", f"faltan {sorted(map(str, faltan))[:5]} · sobran {sorted(map(str, sobran))[:5]}"))
    declaradas = {tuple(o[c] for c in campos) for o in omitidas}
    vacias = set()
    for clave, f in zip(claves, filas, strict=True):
        texto = (f.get("crps") or "").strip()
        if not texto:
            vacias.add(clave)
            continue
        try:
            valor = float(texto)
        except ValueError:
            p.append(("crps-sustancia", f"{clave}: CRPS ilegible {texto!r}"))
            continue
        if not math.isfinite(valor) or valor < 0:
            p.append(("crps-sustancia", f"{clave}: CRPS {texto} no es finito y no negativo"))
    if vacias != declaradas:
        p.append(
            (
                "crps-cobertura",
                f"celdas vacías {sorted(map(str, vacias))[:5]} ≠ omisiones {sorted(map(str, declaradas))[:5]}",
            )
        )
    return p
