"""M74-E-R11 · el sello tiene que ser EXACTAMENTE lo que el árbol deriva, no sólo tener su forma.

Auditoría del 14/15-sep (sin informe escrito): partiendo del sello auténtico de `cb34e87`, entrypoints vacíos,
tres rutas inexistentes con SHA formalmente válido por mapa, semilla 999999 y horizontes vacíos pasaban
(`seal_problems == []`, `publish_blocker is None`), y `NaN` como `learning_rate` también. El esquema conservaba
tipos; nada comparaba contra lo que el preflight deriva de verdad del árbol.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import tools.campaign_manifest as cm  # noqa: E402
from tests.test_sello_campana_m74e_r9 import _arranque, _correr, _plantilla  # noqa: E402

RUNBOOK = RAIZ / "experiments" / "run_rederivation.sh"
RUTAS_FALSAS = ("experiments/no_existe.py", "tools/tampoco.py", "vp_model/fantasma.py")


def _forma_valida(sello: dict, *, vaciar: bool) -> dict:
    """La falsificación reproducida por la auditoría, construida sobre el sello REAL."""
    for k in ("code", "data", "governance"):
        sello["inputs"][k] = dict.fromkeys(RUTAS_FALSAS, "0" * 64)
        sello["counts"][k] = len(RUTAS_FALSAS)
    sello["protocol"]["run_metadata"]["seed"] = 999999
    if vaciar:
        sello["entrypoints"] = {k: [] for k in sello["entrypoints"]}
        sello["protocol"]["horizons"] = []
    return sello


# ═══════════════════════════════ lo que la forma sí puede rechazar, en todos los consumidores
def test_RED_la_falsificacion_de_forma_valida_de_la_auditoria(tmp_path: Path, campana_sellada) -> None:
    m = campana_sellada(tmp_path, sello=_forma_valida(campana_sellada.sello(), vaciar=True))
    problemas = cm.seal_problems(m)
    assert any("sello.entrypoints" in x for x in problemas)
    assert any("sello.protocol.horizons" in x for x in problemas)
    assert cm.publish_blocker(m)


@pytest.mark.parametrize("caso", ["nan", "infinito"])
def test_RED_las_constantes_no_finitas_se_rechazan_al_parsear(tmp_path: Path, campana_sellada, caso: str) -> None:
    sello = campana_sellada.sello()
    sello["protocol"]["run_metadata"]["hyperparams"]["trees"]["learning_rate"] = float(caso[:3])
    crudo = json.dumps(sello).encode("utf-8")  # `allow_nan` por defecto: escribe NaN/Infinity literales
    assert any("no finita" in x for x in cm.seal_problems(campana_sellada(tmp_path, sello=crudo)))


@pytest.mark.parametrize("perturbacion", ["script_repetido", "ruta_absoluta", "ruta_que_sale"])
def test_RED_listas_sin_repetidos_y_rutas_contenidas(tmp_path: Path, campana_sellada, perturbacion: str) -> None:
    sello = campana_sellada.sello()
    if perturbacion == "script_repetido":
        sello["entrypoints"]["scripts"].append(sello["entrypoints"]["scripts"][0])
    else:
        ruta = "/etc/passwd" if perturbacion == "ruta_absoluta" else "data/../../fuera.csv"
        sello["inputs"]["data"][ruta] = "0" * 64
        sello["counts"]["data"] += 1
    assert cm.seal_problems(campana_sellada(tmp_path, sello=sello))


def test_una_lista_numerica_con_repetidos_legitimos_se_sigue_aceptando(tmp_path: Path, campana_sellada) -> None:
    """`seasonal_order` [1, 0, 1, 12] repite un 1 en el sello real: la unicidad aplica a listas de cadenas."""
    sello = campana_sellada.sello()
    assert sello["protocol"]["run_metadata"]["hyperparams"]["sarima"]["seasonal_order"].count(1) > 1
    assert cm.seal_problems(campana_sellada(tmp_path, sello=sello)) == []


# ═══════════════════════════════ lo que sólo la comparación viva puede rechazar: al arrancar, antes de la transacción
def test_RED_el_arranque_compara_el_sello_con_lo_que_el_arbol_deriva(tmp_path: Path, campana_sellada) -> None:
    """★ Sin vaciar nada, la forma es impecable: rutas inexistentes con SHA válido y semilla 999999. Sólo la
    comparación con el preflight que el arranque vuelve a derivar la delata (exit 14)."""
    repo = _arranque(tmp_path, campana_sellada)
    forjado = campana_sellada.sello(head="__HEAD__")
    assert cm.shape_problems(_forma_valida(campana_sellada.sello(), vaciar=False), cm.ESQUEMA["seal"], "sello") == []
    (tmp_path / "forjado.json").write_text(json.dumps(_forma_valida(forjado, vaciar=False)), encoding="utf-8")
    fin = _correr(repo, tmp_path, str(tmp_path / "forjado.json"))
    assert fin.returncode == 14, fin.stdout + fin.stderr
    assert not (repo / "reports" / "campaign" / "PRIMER_PRODUCTOR").exists()


def test_control_el_arranque_con_el_sello_que_el_arbol_deriva(tmp_path: Path, campana_sellada) -> None:
    repo = _arranque(tmp_path, campana_sellada)
    fin = _correr(repo, tmp_path, str(_plantilla(tmp_path, campana_sellada)))
    assert fin.returncode == 0, fin.stdout + fin.stderr
    assert (repo / "reports" / "campaign" / "PRIMER_PRODUCTOR").exists()


def test_la_comparacion_viva_va_tras_acreditar_y_antes_de_la_transaccion() -> None:
    vivo = "\n".join(ln for ln in RUNBOOK.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#"))
    assert vivo.count("-m tools.campaign_preflight --out") == 2
    assert vivo.index("--assert-sealed") < vivo.index("cmp -s") < vivo.index("txn archive")


def test_las_bitacoras_de_campana_no_ensucian_el_arbol_que_el_sello_mide() -> None:
    fin = subprocess.run(["git", "-C", str(RAIZ), "check-ignore", "-q", "reports/logs/rederiv_x.log"], check=False)
    assert fin.returncode == 0
