"""M74-F · C4/C3 — contrato de la transacción complementaria, con mundos sintéticos.

Fija lo que el plan `PLAN_C4_C3_COMPLEMENTARIA_2026-10-08.md` exige ANTES de cualquier cómputo:

* la madre sólo se lee: su `campaign.json` y sus artefactos quedan byte-idénticos tras todo el ciclo;
* las transiciones admitidas, las ilegales y los estados terminales;
* rechazo de artefactos sustituidos, madre/panel distintos, lanes ausentes o sobrantes, semilla
  ajena e identidad de la madre en recibos de la complementaria (nada se re-atribuye);
* una importación se acredita como ``importado:<madre>`` con origen, hashes, panel, protocolo y
  evidencia, y deja de acreditarse si cualquiera cambia o si se disfraza de producida;
* la aceptación de C3: sólo puede cambiar ``baseline_coverage``; gamma y ACI, idénticos;
* la evidencia: cada importación contra el miembro regular y único del tar de la madre, con el tar y la
  identidad (`campaign.json`) que lista su manifiesto; y la DuckDB, ligada al panel y sellada por contenido.

Hermético: directorios temporales, git inyectado, DuckDB temporal construida con el builder real,
ningún productor real.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from tests import m74f_mundo as mm

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "experiments"))

import score_e3_campaign as score  # noqa: E402

from vp_model import complement as cx  # noqa: E402
from vp_model.deck import LaneSpec  # noqa: E402

SHA = "d" * 40
MADRE = "rederiv_953cd00_20261006T073927"
ID = "compl_953cd00_e3_20261009T000000"
DEEP = "reports/eval/deep_pi_FAD.csv"
GLOBAL = "reports/campaign/global_FAD_camp_diff_s1.csv"
EVID_DIR = "reports/campaign/complement_evidence"
EVID = f"{EVID_DIR}/SHA256SUMS"
PROTO = {"table": "FAD", "cqr": False}
#: La función real, antes de que el parche autouse la mida contra RAIZ.
CODIGO_CARGADO = cx.codigo_cargado


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _git_limpio(_root: Path) -> tuple[str, bool]:
    return SHA, False


def _reloj() -> str:
    return "2026-10-09T00:00:00Z"


LANES = [
    LaneSpec("primary-a", "primary", "estable", "FAD", "run_global_deep.py", 1),
    LaneSpec("primary-a", "primary", "estable", "DFF", "run_global_deep.py", 1),
    LaneSpec("control-gbm", "control", "estable", "FAD", "run_global_gbm.py", 1),
    LaneSpec("control-gbm", "control", "estable", "DFF", "run_global_gbm.py", 1),
]


@pytest.fixture(autouse=True)
def _codigo_del_checkout(monkeypatch) -> None:
    """El código de estas pruebas sale de ESTE checkout, no del worktree temporal: `codigo_cargado` se mide
    contra RAIZ. Su conducta frente a un worktree ajeno la prueba `test_m74f_complemento_cableado.py`."""
    monkeypatch.setattr(cx, "codigo_cargado", lambda root, modulos=None: CODIGO_CARGADO(RAIZ, modulos))


@pytest.fixture
def mundo(tmp_path: Path) -> dict:
    madre, compl = tmp_path / "madre", tmp_path / "compl"
    panel = mm.panel_csv()
    for r in (madre, compl):
        (r / "data" / "processed").mkdir(parents=True)
        (r / "data" / "processed" / "visa_panel_long.csv").write_text(panel)
    sello = "sha256:" + _sha(madre / "data" / "processed" / "visa_panel_long.csv")
    (madre / "reports" / "campaign").mkdir(parents=True)
    (madre / "reports" / "campaign" / "campaign.json").write_text(
        json.dumps(
            {
                "campaign_id": MADRE,
                "status": "computed",
                "source_git_sha": "9" * 40,
                "panel_sha256": sello,
                "input_seal_sha256": "e" * 64,
                "revision": 1,
            }
        )  # fmt: skip
    )
    (madre / "reports" / "eval").mkdir(parents=True)
    (madre / DEEP).write_text("unique_id,ds,y,lo,hi\nmx_F1,2025-01-01,1,0,2\n")
    (madre / GLOBAL).write_text("unique_id,ds,BiTCN\nmx_F1,2025-01-01,1.5\n")
    (compl / "reports" / "eval").mkdir(parents=True)
    (compl / "reports" / "campaign").mkdir(parents=True)
    (compl / DEEP).write_bytes((madre / DEEP).read_bytes())
    (compl / GLOBAL).write_bytes((madre / GLOBAL).read_bytes())
    mm.evidencia_madre(compl / EVID_DIR, madre, [DEEP, GLOBAL])
    mm.construir_duckdb(compl / cx.DUCKDB_REL, compl / cx.PANEL_REL, tmp_path / "build")
    (compl / "docs").mkdir()
    (compl / "docs" / "cohort_deck.json").write_text('{"seeds": [1]}\n')
    huellas_madre = {p: _sha(p) for p in madre.rglob("*") if p.is_file()}
    return {"madre": madre, "compl": compl, "huellas_madre": huellas_madre}


def _importacion() -> dict:
    return {"artifact": DEEP, "origin_path": DEEP, "protocol": PROTO,
            "consumers": ["improve_conformal.baseline_coverage", "make_result_figures.F4.base_nf"]}  # fmt: skip


def _imports_c3() -> list[dict]:
    """C3 consume, por tabla, el deep-PI (baseline) y los pronósticos globales: ambos importados."""
    return [
        {**_importacion(), "protocol": cx.artefacto_c3("baseline", "FAD")[1]},
        {"artifact": GLOBAL, "origin_path": GLOBAL, "protocol": cx.artefacto_c3("forecasts", "FAD")[1],
         "consumers": ["improve_conformal._evaluate"]},
    ]  # fmt: skip


def _planificar(m: dict, **kw) -> dict:
    args = {
        "parent_root": m["madre"],
        "complement_id": ID,
        "purpose": "e3",
        "lanes": LANES,
        "seed": 1,
        "frozen_inputs": {"docs/cohort_deck.json": _sha(m["compl"] / "docs" / "cohort_deck.json")},
        "imports": [_importacion()],
        "evidence": EVID,
        "git": _git_limpio,
        "ahora": _reloj,
    }
    return cx.planificar(m["compl"], **{**args, **kw})


def _lanes_escritos(m: dict, doc: dict, mutar=lambda spec, r: r) -> None:
    d = m["compl"] / doc["lanes_dir"]
    d.mkdir(parents=True, exist_ok=True)
    for spec in LANES:
        salida = m["compl"] / "reports" / "eval" / "e3" / f"{spec.recipe}_{spec.table}_{spec.cohort}.csv"
        salida.parent.mkdir(parents=True, exist_ok=True)
        salida.write_text(f"{spec.recipe},{spec.table}\n")
        r = {"recipe": spec.recipe, "recipe_role": spec.role, "cohort": spec.cohort, "table": spec.table,
             "seed": spec.seed, "campaign": cx.identidad(doc),
             "outputs": [{"path": str(salida.relative_to(m["compl"])), "sha256": _sha(salida)}]}  # fmt: skip
        r = mutar(spec, r)
        if r is not None:
            (d / spec.receipt_name).write_text(json.dumps(r))


def _ciclo_hasta_running(m: dict) -> dict:
    _planificar(m)
    return cx.iniciar(m["compl"], parent_root=m["madre"], git=_git_limpio, ahora=_reloj)


def _cerrar(m: dict) -> dict:
    return cx.cerrar_computo(m["compl"], parent_root=m["madre"], acreditar_lanes=score.acreditar_recibos,
                             git=_git_limpio, ahora=_reloj)  # fmt: skip


def _madre_intacta(m: dict) -> None:
    assert {p: _sha(p) for p in m["madre"].rglob("*") if p.is_file()} == m["huellas_madre"]


# ═══════════════════════════════ control válido
def test_ciclo_completo_acredita_y_la_madre_queda_byte_identica(mundo: dict) -> None:
    doc = _ciclo_hasta_running(mundo)
    assert cx.identidad_en_curso(mundo["compl"]) == {"campaign_id": ID, "source_git_sha": SHA,
                                                      "panel_sha256": doc["panel_sha256"]}  # fmt: skip
    _lanes_escritos(mundo, doc)
    assert _cerrar(mundo)["status"] == "computed"
    fin = cx.aceptar(mundo["compl"], parent_root=mundo["madre"], acreditar_lanes=score.acreditar_recibos,
                     git=_git_limpio, ahora=_reloj)  # fmt: skip
    assert fin["status"] == "accredited" and fin["revision"] == 4
    acta = cx.acreditar(mundo["compl"], parent_root=mundo["madre"], acreditar_lanes=score.acreditar_recibos,
                        git=_git_limpio)  # fmt: skip
    assert acta["parent"] == MADRE and acta["lanes"] == 4 and acta["imports"] == [DEEP]
    _madre_intacta(mundo)


def test_la_importacion_valida_se_rotula_importada_de_la_madre(mundo: dict) -> None:
    _ciclo_hasta_running(mundo)
    r = cx.importacion_acreditada(mundo["compl"], DEEP, PROTO, parent_root=mundo["madre"])
    assert r["source"] == f"importado:{MADRE}" and r["sha256"] == _sha(mundo["madre"] / DEEP)
    doc = cx.leer(mundo["compl"])
    assert doc["imports"][0]["accreditation"] == "importado" and doc["imports"][0]["produced_by"] == MADRE


def test_una_complementaria_de_c3_puede_no_tener_lanes(mundo: dict) -> None:
    _c3_en_curso(mundo)
    mundo["huellas_madre"] = {p: _sha(p) for p in mundo["madre"].rglob("*") if p.is_file()}
    assert _cerrar(mundo)["outcome"] == {"lanes": 0, "imports": 2} and cx.leer(mundo["compl"])["lanes"] == []
    sidecar = json.loads((mundo["compl"] / cx.PROCEDENCIA_REL).read_text())
    assert {r["source"] for r in sidecar["tables"]["FAD"].values()} == {f"importado:{MADRE}"}
    _madre_intacta(mundo)


# ═══════════════════════════════ madre, panel y código
@pytest.mark.parametrize("estado", ["running", "failed", "published"])
def test_una_madre_que_no_termino_bien_no_presta_identidad(mundo: dict, estado: str) -> None:
    ruta = mundo["madre"] / cx.CAMPANA_REL
    ruta.write_text(json.dumps({**json.loads(ruta.read_text()), "status": estado}))
    with pytest.raises(cx.ComplementoError) as e:
        _planificar(mundo)
    assert e.value.codigos == {"padre-estado"}
    assert not (mundo["compl"] / cx.RUTA_REL).exists(), "un plan rechazado no escribe nada"


def test_si_la_madre_cambia_despues_del_plan_la_complementaria_falla(mundo: dict) -> None:
    _planificar(mundo)
    ruta = mundo["madre"] / cx.CAMPANA_REL
    ruta.write_text(json.dumps({**json.loads(ruta.read_text()), "revision": 2}))
    with pytest.raises(cx.ComplementoError) as e:
        cx.iniciar(mundo["compl"], parent_root=mundo["madre"], git=_git_limpio, ahora=_reloj)
    assert "padre-cambio" in e.value.codigos
    assert cx.leer(mundo["compl"])["status"] == "failed"


def test_otro_panel_no_planifica(mundo: dict) -> None:
    (mundo["compl"] / cx.PANEL_REL).write_text("otro panel\n")
    with pytest.raises(cx.ComplementoError) as e:
        _planificar(mundo)
    assert "panel" in e.value.codigos


@pytest.mark.parametrize("purpose", ["e3", "deep_pi_conformal"])
def test_entrada_congelada_sustituida_tras_plan_bloquea_inicio(mundo: dict, purpose: str) -> None:
    _planificar(mundo, purpose=purpose, lanes=LANES if purpose == "e3" else [])
    (mundo["compl"] / "docs" / "cohort_deck.json").write_text('{"seeds": [42]}\n')
    with pytest.raises(cx.ComplementoError) as e:
        cx.iniciar(mundo["compl"], parent_root=mundo["madre"], git=_git_limpio, ahora=_reloj)
    assert "entrada-congelada" in e.value.codigos
    assert cx.leer(mundo["compl"])["status"] == "failed"


def test_acreditador_real_relee_entradas_congeladas_tras_computo(mundo: dict) -> None:
    doc = _ciclo_hasta_running(mundo)
    _lanes_escritos(mundo, doc)
    assert _cerrar(mundo)["status"] == "computed"
    (mundo["compl"] / "docs" / "cohort_deck.json").write_text('{"seeds": [42]}\n')
    with pytest.raises(cx.ComplementoError) as e:
        cx.acreditar(
            mundo["compl"], parent_root=mundo["madre"], acreditar_lanes=score.acreditar_recibos, git=_git_limpio
        )
    assert {"entrada-congelada", "lanes"} <= e.value.codigos
    assert cx.leer(mundo["compl"])["status"] == "computed", "el lector rechaza sin escribir"


def test_codigo_sucio_o_head_distinto(mundo: dict) -> None:
    with pytest.raises(cx.ComplementoError) as e:
        _planificar(mundo, git=lambda _r: (SHA, True))
    assert "codigo-sucio" in e.value.codigos
    doc = _ciclo_hasta_running(mundo)
    _lanes_escritos(mundo, doc)
    with pytest.raises(cx.ComplementoError) as e:
        cx.cerrar_computo(mundo["compl"], parent_root=mundo["madre"], acreditar_lanes=score.acreditar_recibos,
                          git=lambda _r: ("f" * 40, False), ahora=_reloj)  # fmt: skip
    assert "codigo" in e.value.codigos


def test_una_campana_completa_en_curso_en_el_mismo_worktree_bloquea(mundo: dict) -> None:
    (mundo["compl"] / "reports" / "campaign").mkdir(parents=True, exist_ok=True)
    (mundo["compl"] / cx.CAMPANA_REL).write_text(json.dumps({"status": "running"}))
    with pytest.raises(cx.ComplementoError) as e:
        _planificar(mundo)
    assert "campana-concurrente" in e.value.codigos


# ═══════════════════════════════ lanes y semillas
def test_semilla_ajena_al_plan_no_planifica(mundo: dict) -> None:
    lanes = [*LANES[:2], LaneSpec("control-gbm", "control", "estable", "FAD", "run_global_gbm.py", 42), LANES[3]]
    with pytest.raises(cx.ComplementoError) as e:
        _planificar(mundo, lanes=lanes)
    assert e.value.codigos == {"semilla"}


@pytest.mark.parametrize(
    ("caso", "mutar", "esperado"),
    [
        ("ausente", lambda s, r: None if s.recipe == "control-gbm" and s.table == "DFF" else r, "falta el recibo"),
        ("semilla 42", lambda s, r: {**r, "seed": 42} if s.recipe == "control-gbm" else r, "seed=42"),
        ("identidad de la madre", lambda s, r: {**r, "campaign": {"campaign_id": MADRE}}, "no es la de la campaña"),
        ("otra receta", lambda s, r: {**r, "recipe": "otra"} if s.table == "FAD" else r, "recipe='otra'"),
    ],
)
def test_lanes_mal_acreditados_dejan_la_complementaria_en_failed(mundo: dict, caso, mutar, esperado) -> None:
    doc = _ciclo_hasta_running(mundo)
    _lanes_escritos(mundo, doc, mutar)
    with pytest.raises(cx.ComplementoError, match=esperado) as e:
        _cerrar(mundo)
    assert e.value.codigos == {"lanes"}, caso
    assert cx.leer(mundo["compl"])["status"] == "failed"


def test_un_lane_sobrante_no_pasa(mundo: dict) -> None:
    doc = _ciclo_hasta_running(mundo)
    _lanes_escritos(mundo, doc)
    (mundo["compl"] / doc["lanes_dir"] / "receipt_intruso_FAD_estable.json").write_text("{}")
    with pytest.raises(cx.ComplementoError, match="fuera del plan"):
        _cerrar(mundo)


def test_una_salida_sustituida_despues_del_lane_no_pasa(mundo: dict) -> None:
    doc = _ciclo_hasta_running(mundo)
    _lanes_escritos(mundo, doc)
    (mundo["compl"] / "reports" / "eval" / "e3" / "primary-a_FAD_estable.csv").write_text("otra cosa\n")
    with pytest.raises(cx.ComplementoError, match="cambió desde que el lane"):
        _cerrar(mundo)


def test_recibos_previos_al_inicio_no_se_cuelan(mundo: dict) -> None:
    doc = _planificar(mundo)
    _lanes_escritos(mundo, doc)
    with pytest.raises(cx.ComplementoError) as e:
        cx.iniciar(mundo["compl"], parent_root=mundo["madre"], git=_git_limpio, ahora=_reloj)
    assert "salidas-previas" in e.value.codigos


def test_editar_el_plan_a_mano_rompe_su_huella(mundo: dict) -> None:
    _planificar(mundo)
    ruta = mundo["compl"] / cx.RUTA_REL
    doc = json.loads(ruta.read_text())
    doc["lanes"] = doc["lanes"][:2]
    ruta.write_text(json.dumps(doc))
    with pytest.raises(cx.ComplementoError) as e:
        cx.leer(mundo["compl"])
    assert e.value.codigos == {"plan-editado"}


def test_un_computed_escrito_a_mano_no_se_acredita_al_leer(mundo: dict) -> None:
    _ciclo_hasta_running(mundo)
    ruta = mundo["compl"] / cx.RUTA_REL
    ruta.write_text(json.dumps({**json.loads(ruta.read_text()), "status": "computed"}))
    with pytest.raises(cx.ComplementoError, match="falta el recibo"):
        cx.acreditar(mundo["compl"], parent_root=mundo["madre"], acreditar_lanes=score.acreditar_recibos,
                     git=_git_limpio)  # fmt: skip


# ═══════════════════════════════ transiciones
def test_transiciones_ilegales_y_estados_terminales(mundo: dict) -> None:
    _planificar(mundo)
    with pytest.raises(cx.ComplementoError) as e:
        _cerrar(mundo)
    assert e.value.codigos == {"transicion"} and cx.leer(mundo["compl"])["status"] == "planned"
    cx.fallar(mundo["compl"], "abortada en prueba", ahora=_reloj)
    for paso in (
        lambda: cx.iniciar(mundo["compl"], parent_root=mundo["madre"], git=_git_limpio, ahora=_reloj),
        lambda: cx.fallar(mundo["compl"], "otra vez", ahora=_reloj),
    ):
        with pytest.raises(cx.ComplementoError) as e:
            paso()
        assert e.value.codigos == {"transicion"}
    with pytest.raises(cx.ComplementoError) as e:
        _planificar(mundo)
    assert e.value.codigos == {"ya-planificada"}, "un reintento es otra complementaria, no la misma reescrita"
    assert all(not v for k, v in cx.TRANSICIONES.items() if k in {"accredited", "failed"})


def test_la_identidad_para_productores_solo_existe_en_running(mundo: dict) -> None:
    _planificar(mundo)
    with pytest.raises(cx.ComplementoError) as e:
        cx.identidad_en_curso(mundo["compl"])
    assert e.value.codigos == {"no-en-curso"}


# ═══════════════════════════════ importaciones
def test_importacion_sustituida_tras_el_plan(mundo: dict) -> None:
    _ciclo_hasta_running(mundo)
    (mundo["compl"] / DEEP).write_text("unique_id,ds,y,lo,hi\nmx_F1,2025-01-01,1,0,9\n")
    with pytest.raises(cx.ComplementoError) as e:
        cx.importacion_acreditada(mundo["compl"], DEEP, PROTO, parent_root=mundo["madre"])
    assert e.value.codigos == {"importacion-sustituida"}


def test_copia_distinta_del_origen_no_planifica(mundo: dict) -> None:
    (mundo["compl"] / DEEP).write_text("copia alterada\n")
    with pytest.raises(cx.ComplementoError) as e:
        _planificar(mundo)
    assert "importacion-sustituida" in e.value.codigos


def test_importacion_con_protocolo_distinto(mundo: dict) -> None:
    _ciclo_hasta_running(mundo)
    with pytest.raises(cx.ComplementoError) as e:
        cx.importacion_acreditada(mundo["compl"], DEEP, {"table": "FAD", "cqr": True}, parent_root=mundo["madre"])
    assert e.value.codigos == {"importacion-protocolo"}


def test_importacion_sin_evidencia_o_con_evidencia_alterada(mundo: dict) -> None:
    with pytest.raises(cx.ComplementoError) as e:
        _planificar(mundo, evidence=None)
    assert e.value.codigos == {"importacion-sin-evidencia"}
    _ciclo_hasta_running(mundo)
    tar = mundo["compl"] / EVID_DIR / cx.EVIDENCIA_ARCHIVO
    tar.write_bytes(tar.read_bytes() + b"\0")
    with pytest.raises(cx.ComplementoError) as e:
        cx.importacion_acreditada(mundo["compl"], DEEP, PROTO, parent_root=mundo["madre"])
    assert e.value.codigos == {"evidencia-cambio"}


def test_importacion_disfrazada_de_producida_no_pasa(mundo: dict) -> None:
    """Un recibo de producción junto al CSV importado lo haría pasar por un deep-PI de la complementaria."""
    (mundo["compl"] / (DEEP + ".receipt.json")).write_text(json.dumps({"campaign_id": ID}))
    with pytest.raises(cx.ComplementoError) as e:
        _planificar(mundo)
    assert "importacion-como-producida" in e.value.codigos


@pytest.mark.parametrize(("campo", "valor", "codigo"), [
    ("produced_by", "rederiv_a6df0f4_20260917T055408", "importacion-otra-campana"),
    ("panel_sha256", "sha256:" + "0" * 64, "importacion-panel"),
])  # fmt: skip
def test_importacion_de_otra_campana_o_panel(mundo: dict, campo: str, valor: str, codigo: str) -> None:
    """La huella del plan cubre las importaciones: editar ``produced_by``/``panel`` rompe el plan; y si
    se reconstruye la huella para esconderlo, el lector lo caza por contenido."""
    _ciclo_hasta_running(mundo)
    ruta = mundo["compl"] / cx.RUTA_REL
    doc = json.loads(ruta.read_text())
    doc["imports"][0][campo] = valor
    doc["plan_sha256"] = cx.huella({k: doc[k] for k in cx._PLAN})
    ruta.write_text(json.dumps(doc))
    with pytest.raises(cx.ComplementoError) as e:
        cx.importacion_acreditada(mundo["compl"], DEEP, PROTO, parent_root=mundo["madre"])
    assert codigo in e.value.codigos


def test_si_el_origen_en_la_madre_cambia_la_importacion_cae(mundo: dict) -> None:
    _ciclo_hasta_running(mundo)
    (mundo["madre"] / DEEP).write_text("la madre ya no es la que se importó\n")
    with pytest.raises(cx.ComplementoError) as e:
        cx.importacion_acreditada(mundo["compl"], DEEP, PROTO, parent_root=mundo["madre"])
    assert e.value.codigos == {"importacion-origen"}


def test_artefacto_no_declarado_no_se_presta(mundo: dict) -> None:
    _ciclo_hasta_running(mundo)
    with pytest.raises(cx.ComplementoError) as e:
        cx.importacion_acreditada(mundo["compl"], "reports/eval/deep_pi_DFF.csv", PROTO, parent_root=mundo["madre"])
    assert e.value.codigos == {"importacion-ausente"}


# ═══════════════════════════════ aceptación de C3
def _conformal(d: Path, base: str, aci: str, gamma: str, *, solo_fad: bool = False) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    dff = "" if solo_fad else "DFF,33,0.867,0.005,0.95\n"
    (d / "conformal_coverage.csv").write_text(
        f"table,n_series,baseline_coverage,aci_gamma,aci_coverage\nFAD,41,{base},0.005,{aci}\n{dff}"
    )
    (d / "aci_gamma.json").write_text(gamma)
    return d


def _c3_en_curso(m: dict, base: str = "0.931", *, sellar: bool = True) -> None:
    """C3 planificada e iniciada con FAD; conformal escribió sus salidas y (si ``sellar``) su procedencia."""
    _conformal(m["madre"] / "reports/eval", "0.890", "0.92", '{"FAD": 0.005}', solo_fad=True)
    _conformal(m["compl"] / "reports/eval", base, "0.92", '{"FAD": 0.005}', solo_fad=True)
    _planificar(m, purpose="deep_pi_conformal", lanes=[], imports=_imports_c3())
    cx.iniciar(m["compl"], parent_root=m["madre"], git=_git_limpio, ahora=_reloj)
    if sellar:
        consumidas = {rol: cx.importacion_acreditada(m["compl"], *cx.artefacto_c3(rol, "FAD"), parent_root=m["madre"])
                      for rol in cx.ROLES_C3}  # fmt: skip
        cx.sellar_procedencia_c3(m["compl"], {"FAD": consumidas}, git=_git_limpio)


def test_c3_acepta_si_solo_cambia_el_baseline(tmp_path: Path) -> None:
    a = _conformal(tmp_path / "madre", "0.890", "0.92", '{"FAD": 0.005}')
    b = _conformal(tmp_path / "c3", "0.931", "0.92", '{"FAD": 0.005}')
    assert cx.diferencias_conformal(a / "conformal_coverage.csv", b / "conformal_coverage.csv") == {
        "DFF": [], "FAD": ["baseline_coverage"]}  # fmt: skip
    assert cx.aceptacion_c3(a, b) == []


def test_c3_rechaza_si_se_mueven_aci_o_gamma(tmp_path: Path) -> None:
    a = _conformal(tmp_path / "madre", "0.890", "0.92", '{"FAD": 0.005}')
    b = _conformal(tmp_path / "c3", "0.931", "0.91", '{"FAD": 0.01}')
    assert {c for c, _ in cx.aceptacion_c3(a, b)} == {"conformal-fuera-de-alcance", "gamma"}


def test_cierre_c3_sin_resultados_no_llega_a_computed(mundo: dict) -> None:
    _planificar(mundo, purpose="deep_pi_conformal", lanes=[])
    cx.iniciar(mundo["compl"], parent_root=mundo["madre"], git=_git_limpio, ahora=_reloj)
    with pytest.raises(cx.ComplementoError) as e:
        _cerrar(mundo)
    assert "conformal-resultados" in e.value.codigos
    assert cx.leer(mundo["compl"])["status"] == "failed"


def test_cierre_c3_hace_obligatoria_la_aceptacion_de_aci_y_gamma(mundo: dict) -> None:
    _conformal(mundo["madre"] / "reports/eval", "0.890", "0.92", '{"FAD": 0.005}')
    _conformal(mundo["compl"] / "reports/eval", "0.931", "0.91", '{"FAD": 0.01}')
    _planificar(mundo, purpose="deep_pi_conformal", lanes=[])
    cx.iniciar(mundo["compl"], parent_root=mundo["madre"], git=_git_limpio, ahora=_reloj)
    with pytest.raises(cx.ComplementoError) as e:
        _cerrar(mundo)
    assert {"conformal-fuera-de-alcance", "gamma"} <= e.value.codigos
    assert cx.leer(mundo["compl"])["status"] == "failed"


def test_lector_c3_rechaza_gamma_sustituido_despues_de_acreditar(mundo: dict) -> None:
    _c3_en_curso(mundo)
    _cerrar(mundo)
    cx.aceptar(mundo["compl"], parent_root=mundo["madre"], acreditar_lanes=score.acreditar_recibos, git=_git_limpio)
    (mundo["compl"] / "reports/eval/aci_gamma.json").write_text('{"FAD": 0.01}')
    with pytest.raises(cx.ComplementoError) as e:
        cx.acreditar(
            mundo["compl"], parent_root=mundo["madre"], acreditar_lanes=score.acreditar_recibos, git=_git_limpio
        )
    assert "gamma" in e.value.codigos
    assert cx.leer(mundo["compl"])["status"] == "accredited", "el lector rechaza sin escribir"


def test_comparacion_c3_no_oculta_filas_duplicadas(tmp_path: Path) -> None:
    a = _conformal(tmp_path / "madre", "0.890", "0.92", '{"FAD": 0.005}')
    b = _conformal(tmp_path / "c3", "0.931", "0.92", '{"FAD": 0.005}')
    csv = b / "conformal_coverage.csv"
    csv.write_text(csv.read_text() + "FAD,41,0.931,0.005,0.92\n")
    with pytest.raises(cx.ComplementoError) as e:
        cx.aceptacion_c3(a, b)
    assert "conformal-filas" in e.value.codigos


# ═══════════════════════════════ procedencia de C3 (sidecar obligatorio)
def _codigos_al_cerrar(m: dict) -> set[str]:
    with pytest.raises(cx.ComplementoError) as e:
        _cerrar(m)
    assert cx.leer(m["compl"])["status"] == "failed"
    return e.value.codigos


def test_c3_sin_sidecar_no_cierra_aunque_solo_cambie_el_baseline(mundo: dict) -> None:
    _c3_en_curso(mundo, sellar=False)
    assert _codigos_al_cerrar(mundo) == {"conformal-procedencia"}


def test_RED_c3_con_baseline_vacio_no_cierra(mundo: dict) -> None:
    """Sin esta guarda, un conformal que no acreditó el deep-PI escribe baseline vacío y sólo esa columna cambia."""
    _c3_en_curso(mundo, base="")
    assert cx.aceptacion_c3(mundo["madre"] / "reports/eval", mundo["compl"] / "reports/eval") == []
    assert _codigos_al_cerrar(mundo) == {"conformal-baseline-vacio"}


def test_sidecar_que_dice_producido_no_acredita(mundo: dict) -> None:
    _c3_en_curso(mundo)
    ruta = mundo["compl"] / cx.PROCEDENCIA_REL
    sc = json.loads(ruta.read_text())
    sc["tables"]["FAD"]["baseline"]["source"] = f"producido:{ID}"
    ruta.write_text(json.dumps(sc))
    assert _codigos_al_cerrar(mundo) == {"conformal-procedencia"}


def test_csv_reescrito_despues_del_sidecar_no_acredita(mundo: dict) -> None:
    _c3_en_curso(mundo)
    _conformal(mundo["compl"] / "reports/eval", "0.932", "0.92", '{"FAD": 0.005}', solo_fad=True)
    assert _codigos_al_cerrar(mundo) == {"conformal-procedencia"}


def test_la_procedencia_solo_se_sella_en_curso(mundo: dict) -> None:
    _c3_en_curso(mundo)
    _cerrar(mundo)
    with pytest.raises(cx.ComplementoError) as e:
        cx.sellar_procedencia_c3(mundo["compl"], {}, git=_git_limpio)
    assert e.value.codigos == {"no-en-curso"}


# ═══════════════════════════════ evidencia de la madre: tar, manifiesto e identidad
def _evidencia(m: dict, artefactos: list[str], **kw) -> None:
    """Rehace la evidencia copiada en la complementaria (antes del plan) con otro tar."""
    import shutil

    shutil.rmtree(m["compl"] / EVID_DIR)
    mm.evidencia_madre(m["compl"] / EVID_DIR, m["madre"], artefactos, **kw)


def test_el_plan_ata_cada_importacion_a_su_miembro_del_tar_de_la_madre(mundo: dict) -> None:
    doc = _planificar(mundo, purpose="deep_pi_conformal", lanes=[], imports=_imports_c3())
    ev = doc["evidence"]
    assert ev["archive"]["path"] == f"{EVID_DIR}/{cx.EVIDENCIA_ARCHIVO}"
    assert ev["identity"]["sha256"] == doc["parent"]["campaign_json_sha256"]
    assert [i["evidence"] for i in doc["imports"]] == [
        {"archive": ev["archive"]["path"], "member": a} for a in (DEEP, GLOBAL)
    ]
    hallados = cx.miembros_tar(mundo["compl"] / ev["archive"]["path"], [DEEP, GLOBAL])
    assert hallados == {
        DEEP: [(True, _sha(mundo["madre"] / DEEP))],
        GLOBAL: [(True, _sha(mundo["madre"] / GLOBAL))],
    }, "el hermano AppleDouble ._deep_pi_FAD.csv no cuenta como miembro"


@pytest.mark.parametrize(("caso", "codigo"), [
    ("ausente", "importacion-tar-ausente"),
    ("duplicado", "importacion-tar-duplicado"),
    ("no_regular", "importacion-tar-no-regular"),
    ("distinto", "importacion-tar-distinto"),
])  # fmt: skip
def test_RED_miembro_del_tar_que_no_acredita_no_planifica(mundo: dict, caso: str, codigo: str) -> None:
    if caso == "ausente":
        _evidencia(mundo, [GLOBAL])
    elif caso == "duplicado":  # «./ruta» y «ruta» son el mismo miembro para tar
        _evidencia(mundo, [DEEP, GLOBAL], extra={f"./{DEEP}": (mundo["madre"] / DEEP).read_bytes()})
    elif caso == "no_regular":
        _evidencia(mundo, [GLOBAL], enlaces={DEEP: "../../otra/deep_pi_FAD.csv"})
    else:
        _evidencia(mundo, [DEEP, GLOBAL], extra={DEEP: b"unique_id,ds,y,lo,hi\nmx_F1,2025-01-01,1,0,9\n"})
    with pytest.raises(cx.ComplementoError) as e:
        _planificar(mundo)
    assert e.value.codigos == {codigo}
    assert not (mundo["compl"] / cx.RUTA_REL).exists()


@pytest.mark.parametrize(("caso", "codigo"), [
    ("tar_no_listado", "evidencia-archivo"),
    ("identidad_ajena", "evidencia-identidad"),
    ("manifiesto_fuera_de_reports", "evidencia-manifiesto"),
    ("manifiesto_sin_el_tar", "evidencia-manifiesto"),
])  # fmt: skip
def test_RED_evidencia_sin_hash_o_sin_identidad_de_la_madre_no_planifica(mundo: dict, caso: str, codigo: str) -> None:
    carpeta = mundo["compl"] / EVID_DIR
    evidencia = EVID
    if caso == "tar_no_listado":
        (carpeta / cx.EVIDENCIA_ARCHIVO).write_bytes((carpeta / cx.EVIDENCIA_ARCHIVO).read_bytes() + b"\0")
    elif caso == "identidad_ajena":
        copia = json.loads((carpeta / "campaign.json").read_text())
        (carpeta / "campaign.json").write_text(json.dumps({**copia, "campaign_id": "rederiv_a6df0f4_20260917T055408"}))
        mm.escribir_manifiesto(carpeta)
    elif caso == "manifiesto_fuera_de_reports":
        (mundo["compl"] / "SHA256SUMS").write_bytes((carpeta / "SHA256SUMS").read_bytes())
        evidencia = "SHA256SUMS"
    else:
        sums = (carpeta / "SHA256SUMS").read_text().splitlines()
        (carpeta / "SHA256SUMS").write_text("\n".join(x for x in sums if cx.EVIDENCIA_ARCHIVO not in x) + "\n")
    with pytest.raises(cx.ComplementoError) as e:
        _planificar(mundo, evidence=evidencia)
    assert codigo in e.value.codigos
    assert not (mundo["compl"] / cx.RUTA_REL).exists()


def test_evidencia_sustituida_despues_del_plan_no_inicia(mundo: dict) -> None:
    """Un tar nuevo con manifiesto coherente sigue sin ser el sellado: el plan guarda sus hashes."""
    _planificar(mundo)
    _evidencia(mundo, [DEEP, GLOBAL], extra={"reports/eval/otro.csv": b"x\n"})
    with pytest.raises(cx.ComplementoError) as e:
        cx.iniciar(mundo["compl"], parent_root=mundo["madre"], git=_git_limpio, ahora=_reloj)
    assert e.value.codigos == {"evidencia-cambio"}
    assert cx.leer(mundo["compl"])["status"] == "failed"


# ═══════════════════════════════ DuckDB: ligada al panel, sellada por contenido
def _otra_duckdb(m: dict, **kw) -> None:
    ruta = m["compl"] / cx.DUCKDB_REL
    ruta.unlink()
    mm.construir_duckdb(ruta, kw.pop("panel", m["compl"] / cx.PANEL_REL), m["compl"].parent / "build2", **kw)


def test_el_plan_sella_la_duckdb_por_contenido_y_su_panel(mundo: dict) -> None:
    """La huella de `vp_data` (la que `vp_model` puede importar) ES la del contrato de determinismo."""
    from pipeline.db_governance import content_fingerprint
    from vp_data.duckdb_huella import huella_catalogo, huella_contenido

    assert huella_contenido(mundo["compl"] / cx.DUCKDB_REL) == content_fingerprint(mundo["compl"] / cx.DUCKDB_REL)
    doc = _planificar(mundo)
    assert doc["duckdb"] == {
        "path": cx.DUCKDB_REL,
        "content_sha256": content_fingerprint(mundo["compl"] / cx.DUCKDB_REL),
        "catalog_sha256": huella_catalogo(mundo["compl"] / cx.DUCKDB_REL),
        "etl_panel_sha256": doc["panel_sha256"],
        "build_status": "ok",
    }


@pytest.mark.parametrize(("caso", "codigo"), [
    ("ausente", "duckdb-ausente"),
    ("de_otro_panel", "duckdb-panel"),
    ("degradada", "duckdb-degradada"),
    ("build_interrumpido", "duckdb-incompleta"),
])  # fmt: skip
def test_RED_duckdb_no_ligada_al_panel_no_planifica(mundo: dict, tmp_path: Path, caso: str, codigo: str) -> None:
    if caso == "ausente":
        (mundo["compl"] / cx.DUCKDB_REL).unlink()
    elif caso == "de_otro_panel":
        otro = tmp_path / "otro_panel.csv"
        otro.write_text(mm.panel_csv() + "mexico,family,F1,FAD,2030-01-01,F,,,\n")
        _otra_duckdb(mundo, panel=otro)
    elif caso == "degradada":
        _otra_duckdb(mundo, degradada=True)
    else:  # etl_run se escribe al FINAL del build: sin su fila, el almacén quedó a medias
        import duckdb

        con = duckdb.connect(str(mundo["compl"] / cx.DUCKDB_REL))
        con.execute("DELETE FROM etl_run")
        con.close()
    with pytest.raises(cx.ComplementoError) as e:
        _planificar(mundo)
    assert e.value.codigos == {codigo}
    assert not (mundo["compl"] / cx.RUTA_REL).exists()


def test_RED_duckdb_reconstruida_con_otro_contenido_no_inicia(mundo: dict) -> None:
    _planificar(mundo)
    _otra_duckdb(mundo, meses=29)
    with pytest.raises(cx.ComplementoError) as e:
        cx.iniciar(mundo["compl"], parent_root=mundo["madre"], git=_git_limpio, ahora=_reloj)
    assert e.value.codigos == {"duckdb-cambio"}
    assert cx.leer(mundo["compl"])["status"] == "failed"


def test_control_duckdb_reconstruida_igual_con_otros_bytes_sigue_acreditando(mundo: dict) -> None:
    """El .duckdb no es byte-reproducible: lo que se sella es el contenido lógico, no los bytes."""
    _planificar(mundo)
    antes = _sha(mundo["compl"] / cx.DUCKDB_REL)
    _otra_duckdb(mundo, started_at="2026-10-08T12:00:00Z")
    assert _sha(mundo["compl"] / cx.DUCKDB_REL) != antes
    assert cx.iniciar(mundo["compl"], parent_root=mundo["madre"], git=_git_limpio, ahora=_reloj)["status"] == "running"


def test_el_lector_relee_la_duckdb_despues_de_acreditar(mundo: dict) -> None:
    doc = _ciclo_hasta_running(mundo)
    _lanes_escritos(mundo, doc)
    _cerrar(mundo)
    cx.aceptar(mundo["compl"], parent_root=mundo["madre"], acreditar_lanes=score.acreditar_recibos,
               git=_git_limpio, ahora=_reloj)  # fmt: skip
    _otra_duckdb(mundo, git_sha="b" * 40)
    with pytest.raises(cx.ComplementoError) as e:
        cx.acreditar(mundo["compl"], parent_root=mundo["madre"], acreditar_lanes=score.acreditar_recibos,
                     git=_git_limpio)  # fmt: skip
    assert e.value.codigos == {"duckdb-cambio"}
    assert cx.leer(mundo["compl"])["status"] == "accredited", "el lector rechaza sin escribir"


def _lectura_consumidor(db: Path, vista: str) -> object:
    """Lo que el consumidor REAL (`vp_model.dataset`) recibe de ``vista``."""
    from vp_model import dataset

    if vista == "mart_training_F":
        return dataset.actuals_F(db_path=db)
    return dataset.list_series(db_path=db)["n_trainable"].tolist()


@pytest.mark.parametrize("vista", sorted(mm.CAMBIOS_DE_VISTA))
def test_RED_vista_redefinida_tras_el_plan_no_inicia(mundo: dict, vista: str) -> None:
    """El RED de la adenda Codex: mismas filas, otra semántica. La huella de filas no lo ve; el catálogo sí."""
    _planificar(mundo)
    db = mundo["compl"] / cx.DUCKDB_REL
    antes, leido = cx.sello_duckdb(db), _lectura_consumidor(db, vista)
    mm.redefinir_vista(db, vista)
    despues = cx.sello_duckdb(db)
    assert _lectura_consumidor(db, vista) != leido, "el consumidor recibe otra cosa"
    assert despues["content_sha256"] == antes["content_sha256"], "las filas son las mismas"
    with pytest.raises(cx.ComplementoError) as e:
        cx.iniciar(mundo["compl"], parent_root=mundo["madre"], git=_git_limpio, ahora=_reloj)
    assert e.value.codigos == {"duckdb-catalogo"}
    assert cx.leer(mundo["compl"])["status"] == "failed"


@pytest.mark.parametrize("vista", sorted(mm.CAMBIOS_DE_VISTA))
def test_RED_vista_redefinida_en_curso_no_calcula(mundo: dict, vista: str) -> None:
    _ciclo_hasta_running(mundo)
    db = mundo["compl"] / cx.DUCKDB_REL
    mm.redefinir_vista(db, vista)
    with pytest.raises(cx.ComplementoError) as e:
        cx.precondiciones_calculo(mundo["compl"], proposito="e3", duckdb_consumida=db, git=_git_limpio)
    assert e.value.codigos == {"duckdb-catalogo"}


def test_RED_vista_redefinida_tras_acreditar_no_se_lee(mundo: dict) -> None:
    doc = _ciclo_hasta_running(mundo)
    _lanes_escritos(mundo, doc)
    _cerrar(mundo)
    cx.aceptar(mundo["compl"], parent_root=mundo["madre"], acreditar_lanes=score.acreditar_recibos,
               git=_git_limpio, ahora=_reloj)  # fmt: skip
    mm.redefinir_vista(mundo["compl"] / cx.DUCKDB_REL, "mart_series_summary")
    with pytest.raises(cx.ComplementoError) as e:
        cx.acreditar(mundo["compl"], parent_root=mundo["madre"], acreditar_lanes=score.acreditar_recibos,
                     git=_git_limpio)  # fmt: skip
    assert e.value.codigos == {"duckdb-catalogo"}
    assert cx.leer(mundo["compl"])["status"] == "accredited", "el lector rechaza sin escribir"


def test_control_catalogo_igual_en_otro_build_y_sin_sello_previo_falla_cerrado(mundo: dict) -> None:
    """Dos builds con las mismas entradas: mismo catálogo. Un plan sin `catalog_sha256` no se da por bueno."""
    from vp_data.duckdb_huella import huella_catalogo

    db = mundo["compl"] / cx.DUCKDB_REL
    antes = huella_catalogo(db)
    _otra_duckdb(mundo, started_at="2026-10-08T12:00:00Z")
    assert huella_catalogo(db) == antes
    doc = _planificar(mundo)
    sin = {**doc, "duckdb": {k: v for k, v in doc["duckdb"].items() if k != "catalog_sha256"}}
    assert {c for c, _ in cx._verificar_duckdb(mundo["compl"], sin)} == {"duckdb-catalogo"}


def test_un_productor_que_abriria_otra_duckdb_no_calcula(mundo: dict, tmp_path: Path) -> None:
    _ciclo_hasta_running(mundo)
    ajena = mm.construir_duckdb(tmp_path / "otro_cwd" / cx.DUCKDB_REL, mundo["compl"] / cx.PANEL_REL, tmp_path / "b3")
    with pytest.raises(cx.ComplementoError) as e:
        cx.precondiciones_calculo(mundo["compl"], proposito="e3", duckdb_consumida=ajena, git=_git_limpio)
    assert e.value.codigos == {"duckdb-ajena"}, "aunque su contenido sea idéntico, no es la sellada"
    doc = cx.precondiciones_calculo(
        mundo["compl"], proposito="e3", duckdb_consumida=mundo["compl"] / cx.DUCKDB_REL, git=_git_limpio
    )
    assert doc["status"] == "running"


# ═══════════════════════════════ código permitido (git inyectado; el real, en el cableado)
def test_codigo_permitido_es_la_unica_comprobacion_y_cubre_head_y_suciedad() -> None:
    assert cx.codigo_permitido(Path("."), SHA, lambda _r: (SHA, False)) == (SHA, [])
    assert cx.codigo_permitido(Path("."), SHA, lambda _r: (SHA, True))[1] == [
        ("codigo-sucio", "hay cambios sin commitear fuera de reports/")
    ]
    codigos = {c for c, _ in cx.codigo_permitido(Path("."), SHA, lambda _r: ("f" * 40, True))[1]}
    assert codigos == {"codigo", "codigo-sucio"}
    assert cx.codigo_permitido(Path("."), None, lambda _r: ("f" * 40, False))[1] == [], "al planificar no hay HEAD"


def test_codigo_cargado_de_otro_checkout_es_ajeno(tmp_path: Path) -> None:
    from types import SimpleNamespace

    dentro = SimpleNamespace(__file__=str(tmp_path / "vp_model" / "__init__.py"))
    fuera = SimpleNamespace(__file__="/otro/checkout/vp_model/__init__.py")
    medir = CODIGO_CARGADO
    assert medir(tmp_path, {"vp_model": dentro, "vp_data": dentro}) == []
    assert [c for c, _ in medir(tmp_path, {"vp_model": fuera})] == ["codigo-ajeno"]
    assert [c for c, _ in medir(tmp_path, {"pipeline": SimpleNamespace()})] == ["codigo-ajeno"], "sin __file__"
