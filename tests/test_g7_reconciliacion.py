"""M74-F · G7 — recibo de reconciliación: la madre y sus complementarias acreditadas, en un mismo árbol.

RED de conducta (real, 14-oct, sobre `c1cbe2b`): el CSV y el recibo de C5c y de C6, copiados a otro árbol,
salen ``heredado`` («ninguna transacción de este árbol lo acredita»), y el veredicto campeón no se puede
re-derivar sobre la madre `computed` (`read_accredited` exige una campaña `running`). Ninguna cifra
corregida por las complementarias podía llegar a un canónico.

Integración con las APIs REALES: contrato (`vp_model.reconciliation`), CLI `run_reconciliation`, las
complementarias C5 y C6 completas con sus CLI y productores (sólo el cálculo por serie es doble), C4 con el
orquestador y el puntuador reales (los lanes son dobles), git real en clones del mismo commit y la DuckDB
del builder real. La madre y su evidencia tienen la forma de las reales (tar con AppleDouble).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

import tests.test_m74f_champion_crps as c5
import tests.test_m74f_complemento_cableado as cab
import tests.test_m74f_crps_baseline_contrato as c6
from tests import m74f_mundo as mm

mundo, m5, _codigo_del_checkout = c5.mundo, c5.m5, c5._codigo_del_checkout

pytest.importorskip("darts")

import build_key_facts as bkf  # noqa: E402
import run_champion_challenger as rcc  # noqa: E402
import run_e3_campaign as e3  # noqa: E402
import run_reconciliation as cli  # noqa: E402
import score_e3_campaign as score  # noqa: E402

from vp_model import artifact_receipt as ar  # noqa: E402
from vp_model import champion  # noqa: E402
from vp_model import complement as cx  # noqa: E402
from vp_model import reconciliation as rc  # noqa: E402

ID = "recon_g7_t1"
EVID_DIR = cab.EVID_DIR
EVID = cab.EVID
POOL = "reports/campaign/campaign_pool_FAD_family.csv"
VEREDICTO = "reports/governance/champion_challenger.json"


def _veredicto_madre() -> dict:
    """El veredicto de la madre real: evaluación hecha, CRPS heredado (el CSV de julio, sin recibo)."""
    retador = {"challenger": "naive1", "mean": 0.1, "median": 0.09, "n_series": 7, "mean_margin_vs_champion": 0.0,
               "wilcoxon_p": 1.0, "holm_p": 1.0, "holdout_pass": False, "promotable": False,
               "recipe": {"models": ["naive1"], "agg": "median"}}  # fmt: skip
    por_tabla = {
        t: {"champion": "median(theta+ets+sarima)", "champion_mean": 0.121, "champion_median": 0.1,
            "champion_crps": None, "challengers": [retador], "gate_scope": "retrospective-holdout-h1",
            "holdout_winner": None, "promote": None,
            "champion_crps_provenance": {"status": "heredado", "value": None, "inherited_value": 32.1,
                                         "artifact_sha256": "f" * 64, "reason": "sin recibo"}}
        for t in ("FAD", "DFF")
    }  # fmt: skip
    return {**por_tabla, "campaign_id": cab.MADRE, "git_sha": "9" * 40}


#: Lo que la madre re-derivó y su tar sella (además de lo que C4 importa de ella).
SALIDAS_MADRE = {
    VEREDICTO: json.dumps(_veredicto_madre(), indent=2) + "\n",
    "reports/governance/key_facts.json": '{"crps_fad": 32.1}\n',
    "reports/eval/conformal_coverage.csv": "table,baseline_coverage,split_coverage,aci_coverage\nFAD,0.89,0.9,0.87\n",
    "reports/eval/crps_fad.csv": "model,country,category,crps\narima,mexico,F1,10\n",
    POOL: "model,hold_mase\nets,0.1\n",
}


def _clonar(origen: Path, destino: Path) -> Path:
    """Otro worktree del MISMO commit, con la DuckDB (ignorada por git) copiada byte a byte."""
    subprocess.run(["git", "clone", "-q", str(origen), str(destino)], check=True, capture_output=True)
    shutil.copyfile(origen / cx.DUCKDB_REL, destino / cx.DUCKDB_REL)
    return destino


def _miembros_madre(madre: Path) -> list[str]:
    return [*SALIDAS_MADRE, "reports/eval/cohort_scan.json", *(f"reports/eval/{n}" for n in score.SCORECARDS)]


@pytest.fixture
def rm(m5: dict, tmp_path: Path, monkeypatch, capsys) -> dict:
    """Madre `computed` con sus salidas; C5 (árbol A) y C6 (clon B) `accredited`; árbol R sin ensamblar."""
    madre, a = m5["madre"], m5["compl"]
    for rel, texto in SALIDAS_MADRE.items():
        cab._escribir(madre, rel, texto)
    c5._c5_calculada(m5, monkeypatch, capsys)
    assert c5._cerrar(m5, capsys)[1]["status"] == "computed"
    assert cab._cli(m5, "accept", "--parent-root", str(madre), capsys=capsys)[1]["status"] == "accredited"
    b = _clonar(a, tmp_path / "c6")
    c6._aceptada({"compl": b, "madre": madre}, monkeypatch, capsys)
    r = _clonar(a, tmp_path / "recon")
    mm.evidencia_madre(r / EVID_DIR, madre, _miembros_madre(madre))
    monkeypatch.chdir(r)
    return {"madre": madre, "c5": a, "c6": b, "recon": r, "tmp": tmp_path}


def _ensamblar(m: dict, capsys, *fuentes: Path, raiz: Path | None = None) -> tuple[int, dict]:
    capsys.readouterr()
    fuentes = fuentes or (m["c5"], m["c6"])
    args = ["--root", str(raiz or m["recon"]), "assemble", "--parent-root", str(m["madre"]), "--evidence", EVID,
            "--id", ID, *(x for f in fuentes for x in ("--source", str(f)))]  # fmt: skip
    rc_ = cli.main(args)
    return rc_, json.loads(capsys.readouterr().out)


def _ensamblado(m: dict, capsys) -> dict:
    rc_, out = _ensamblar(m, capsys)
    assert rc_ == 0 and out["status"] == "assembled", out
    return out


def _crps(m: dict, tabla: str = "FAD", **kw) -> dict:
    r = m["recon"]
    kw.setdefault("parent_root", m["madre"])
    return champion.crps_provenance(tabla, reports=r / "reports", panel=r / cx.PANEL_REL, **kw)


def _base(m: dict, **kw) -> dict:
    r = m["recon"]
    kw.setdefault("parent_root", m["madre"])
    return champion.baseline_crps_provenance(reports=r / "reports", panel=r / cx.PANEL_REL, **kw)


def _codigos(m: dict, **kw) -> set[str]:
    try:
        rc.acreditar(m["recon"], parent_root=m["madre"], **kw)
    except rc.ReconciliacionError as exc:
        return exc.codigos
    return set()


def _sha(p: Path) -> str:
    return ar.sha256_file(p)


# ═══════════════════════════════ controles: el ciclo real
def test_control_ensamblar_acredita_el_crps_de_las_dos_fuentes(rm: dict, capsys) -> None:
    out = _ensamblado(rm, capsys)
    r = rm["recon"]
    assert set(out["sources"]) == {c5.ID_C5, c6.ID_C6}
    for fuente, rel in ((rm["c5"], cx.CRPS_REL), (rm["c6"], cx.CRPS_BASE_REL)):
        for f in (rel, str(ar.receipt_path(Path(rel)))):
            assert _sha(r / f) == _sha(fuente / f), f"{f}: la copia es la de su fuente"
    assert _sha(r / POOL) == _sha(rm["madre"] / POOL), "capa de la madre extraída del tar"
    referencia = rc.referencia(r, VEREDICTO)
    assert _sha(referencia) == _sha(rm["madre"] / VEREDICTO) and not (r / VEREDICTO).exists()
    doc = rc.leer(r)
    assert {"member": cx.CRPS_BASE_REL, "reason": "lo sustituye una fuente"} in doc["excluded"]
    assert cx.CRPS_BASE_REL not in doc["mother_layers"], "una ruta, una sola autoridad"
    assert any(e["member"].endswith("._campaign_pool_FAD_family.csv") for e in doc["excluded"])

    for tabla in ("FAD", "DFF"):
        p = _crps(rm, tabla)
        assert p["status"] == "acreditado" and p["receipt_campaign_id"] == c5.ID_C5, p
        assert p["transaction"] == {"kind": "reconciliation", "status": "assembled", "reconciliation_id": ID,
                                    "source": c5.ID_C5}  # fmt: skip
    b = _base(rm)
    fuente = champion.baseline_crps_provenance(reports=rm["c6"] / "reports", panel=rm["c6"] / cx.PANEL_REL,
                                               parent_root=rm["madre"])  # fmt: skip
    assert b["status"] == "acreditado" and b["means"] == fuente["means"] and fuente["status"] == "acreditado"
    assert cli.main(["--root", str(r), "verify", "--parent-root", str(rm["madre"])]) == 0


def test_control_el_veredicto_de_la_madre_con_el_crps_acreditado_llega_a_key_facts(
    rm: dict, capsys, monkeypatch
) -> None:
    _ensamblado(rm, capsys)
    r = rm["recon"]
    monkeypatch.setattr(rcc, "REPORTS", r / "reports")
    assert rcc.main(["--reconciliation", "--crps-parent-root", str(rm["madre"])]) == 0
    madre, nuevo = _veredicto_madre(), json.loads((r / VEREDICTO).read_text())
    assert nuevo["reconciliation_id"] == ID and nuevo["campaign_id"] == cab.MADRE
    for t in ("FAD", "DFF"):
        prov = nuevo[t]["champion_crps_provenance"]
        assert prov["status"] == "acreditado" and nuevo[t]["champion_crps"] == prov["value"] is not None
        sin_crps = {k: v for k, v in nuevo[t].items() if not k.startswith("champion_crps")}
        assert sin_crps == {k: v for k, v in madre[t].items() if not k.startswith("champion_crps")}
    monkeypatch.setattr(bkf, "REPORTS", r / "reports")
    hechos = bkf._champion_challenger()
    assert hechos["crps_estado"] == "acreditado" and "crps_fad_heredado" not in hechos
    assert hechos["crps_fad"] == round(nuevo["FAD"]["champion_crps"], 1)
    assert _codigos(rm) == set(), "el veredicto es una salida declarada: la reconciliación sigue acreditada"


def test_control_cerrar_sella_las_salidas_y_despues_no_pueden_cambiar(rm: dict, capsys) -> None:
    _ensamblado(rm, capsys)
    r = rm["recon"]
    for rel in rc.SALIDAS:
        cab._escribir(r, rel, f"re-derivada {rel}\n")
    cab._escribir(r, f"{rc.FIGURAS}results_coverage_crps.pdf", "%PDF\n")
    assert cli.main(["--root", str(r), "close", "--parent-root", str(rm["madre"])]) == 0
    doc = rc.leer(r)
    assert doc["status"] == "derived" and set(doc["outputs"]) == {*rc.SALIDAS, f"{rc.FIGURAS}results_coverage_crps.pdf"}
    assert _crps(rm)["status"] == "acreditado"
    cab._escribir(r, "reports/latex/key_facts.tex", "otra\n")
    assert "salida-cambio" in _codigos(rm)
    cab._escribir(r, "reports/latex/key_facts.tex", "re-derivada reports/latex/key_facts.tex\n")
    cab._escribir(r, f"{rc.FIGURAS}tardia.pdf", "%PDF\n")
    assert _codigos(rm) == {"salida-nueva"}


def test_cerrar_sin_todas_las_salidas_falla_y_es_terminal(rm: dict, capsys) -> None:
    _ensamblado(rm, capsys)
    assert cli.main(["--root", str(rm["recon"]), "close", "--parent-root", str(rm["madre"])]) == 2
    doc = rc.leer(rm["recon"])
    assert doc["status"] == "failed" and doc["failure"]["codes"] == ["salida-ausente"]
    p = _crps(rm)
    assert p["status"] == "heredado" and "failed" in p["reason"], p


# ═══════════════════════════════ lectura: nada se cree, todo se re-verifica
def _resellar_crps(raiz: Path) -> None:
    """Cambia un CRPS y RE-SELLA el recibo coherente: la sustancia pasa; sólo la reconciliación lo caza."""
    filas = c5._filas({"compl": raiz})
    filas[0]["crps"] = str(float(filas[0]["crps"]) + 5)
    c5._reescribir({"compl": raiz}, filas)


def _resellar_plan(raiz: Path, cambio) -> None:
    ruta = raiz / rc.RUTA_REL
    doc = json.loads(ruta.read_text())
    cambio(doc)
    doc["plan_sha256"] = cx.huella({k: doc[k] for k in rc._PLAN})
    ruta.write_text(json.dumps(doc, indent=2, sort_keys=True))


def _plan_y_arbol(m: dict) -> None:
    """El CSV del árbol cambiado y el plan re-sellado a mano para que lo describa: la FUENTE no lo respalda."""
    _resellar_crps(m["recon"])
    nuevo = {rel: _sha(m["recon"] / rel) for rel in (cx.CRPS_REL, str(ar.receipt_path(Path(cx.CRPS_REL))))}
    _resellar_plan(m["recon"], lambda d: next(s for s in d["sources"] if s["purpose"] == "champion_crps")[
        "artifacts"].update(nuevo))  # fmt: skip


def _capa_y_plan(m: dict) -> None:
    """Una capa de la madre cambiada con el plan re-sellado: el tar sellado no la respalda."""
    cab._escribir(m["recon"], POOL, "model,hold_mase\nets,0.01\n")
    _resellar_plan(m["recon"], lambda d: d["mother_layers"].update({POOL: _sha(m["recon"] / POOL)}))


MUTACIONES = {
    "csv-del-arbol-resellado": (lambda m: _resellar_crps(m["recon"]), "capa-cambio"),
    "csv-de-la-fuente-resellado": (lambda m: _resellar_crps(m["c5"]), "fuente-artefacto"),
    "csv-de-la-fuente-sin-resellar": (
        lambda m: (m["c5"] / cx.CRPS_REL).write_text((m["c5"] / cx.CRPS_REL).read_text() + "\n"),
        "fuente-no-acreditada",
    ),
    "plan-editado": (
        lambda m: (m["recon"] / rc.RUTA_REL).write_text(
            (m["recon"] / rc.RUTA_REL)
            .read_text()
            .replace('"status": "assembled"', '"status": "derived"')
            .replace(c5.ID_C5, "compl_otro")
        ),
        "plan-editado",
    ),
    "plan-y-arbol-resellados": (_plan_y_arbol, "fuente-artefacto"),
    "capa-de-la-madre": (lambda m: cab._escribir(m["recon"], POOL, "otra\n"), "capa-cambio"),
    "capa-y-plan-resellados": (_capa_y_plan, "capa-tar"),
    "intruso-en-reports": (lambda m: cab._escribir(m["recon"], "reports/eval/intruso.csv", "x\n"), "inventario"),
    "capa-borrada": (lambda m: (m["recon"] / POOL).unlink(), "capa-cambio"),
    "codigo-sucio": (lambda m: cab._escribir(m["recon"], "experiments/codigo.py", "X = 2\n"), "codigo-sucio"),
    "commit-nuevo": (
        lambda m: (
            cab._escribir(m["recon"], "experiments/otro.py", "Y = 1\n"),
            cab._git(m["recon"], "add", "-A"),
            cab._git(m["recon"], "commit", "-qm", "x"),
        ),
        "codigo",
    ),
    "madre-cambiada": (
        lambda m: (m["madre"] / cx.CAMPANA_REL).write_text(
            (m["madre"] / cx.CAMPANA_REL).read_text().replace('"revision": 7', '"revision": 8')
        ),
        "padre-cambio",
    ),
    "evidencia-tocada": (
        lambda m: (m["recon"] / EVID).write_text((m["recon"] / EVID).read_text() + "\n"),
        "evidencia-cambio",
    ),
    "complement-json-de-la-fuente": (
        lambda m: (m["c5"] / cx.RUTA_REL).write_text(
            (m["c5"] / cx.RUTA_REL).read_text().replace('"revision": 4', '"revision": 5')
        ),
        "fuente-cambio",
    ),
    "campana-en-el-arbol": (lambda m: cab._escribir(m["recon"], str(cx.CAMPANA_REL), "{}\n"), "transaccion-ajena"),
    "complementaria-en-el-arbol": (
        lambda m: cab._escribir(m["recon"], str(cx.RUTA_REL), "{}\n"),
        "transaccion-ajena",
    ),
    "duckdb-reconstruida": (
        lambda m: (
            (m["recon"] / cx.DUCKDB_REL).unlink(),
            mm.construir_duckdb(
                m["recon"] / cx.DUCKDB_REL, m["recon"] / cx.PANEL_REL, m["tmp"] / "otra_build", meses=24
            ),
        ),
        "duckdb-cambio",
    ),
}


@pytest.mark.parametrize("caso", sorted(MUTACIONES))
def test_lo_que_cambia_despues_de_ensamblar_no_se_acredita(rm: dict, capsys, caso: str) -> None:
    _ensamblado(rm, capsys)
    assert _crps(rm)["status"] == "acreditado", "control antes de mutar"
    mutar, codigo = MUTACIONES[caso]
    mutar(rm)
    p = _crps(rm)
    assert p["status"] == "heredado" and p["value"] is None, p
    assert codigo in _codigos(rm, solo=c5.ID_C5), (codigo, p["reason"])
    assert cli.main(["--root", str(rm["recon"]), "verify", "--parent-root", str(rm["madre"])]) == 2


def test_sin_la_raiz_de_la_madre_no_se_acredita(rm: dict, capsys) -> None:
    _ensamblado(rm, capsys)
    p = _crps(rm, parent_root=None)
    assert p["status"] == "heredado" and "parent_root" in p["reason"], p


def test_un_recibo_que_no_es_de_ninguna_fuente_no_se_acredita(rm: dict, capsys) -> None:
    """Sólo C6 es fuente; el CSV de C5 llega al árbol sin pasar por el ensamblador."""
    rc_, out = _ensamblar(rm, capsys, rm["c6"])
    assert rc_ == 0, out
    for f in (cx.CRPS_REL, str(ar.receipt_path(Path(cx.CRPS_REL)))):
        cab._escribir(rm["recon"], f, (rm["c5"] / f).read_text())
    p = _crps(rm)
    assert p["status"] == "heredado" and "ninguna fuente" in p["reason"], p
    b = _base(rm)
    assert b["status"] == "heredado" and "inventario" in b["reason"], "los archivos colados ensucian el árbol entero"


def test_una_fuente_de_otro_proposito_no_responde_por_el_crps(rm: dict, capsys, monkeypatch) -> None:
    _ensamblado(rm, capsys)
    leer = rc.leer

    def cruzado(root):
        doc = leer(root)
        for s in doc["sources"]:
            s["purpose"] = "baseline_crps" if s["purpose"] == "champion_crps" else "champion_crps"
        return doc

    monkeypatch.setattr(rc, "leer", cruzado)
    p = _crps(rm)
    assert p["status"] == "heredado" and "no responde por un CRPS" in p["reason"], p


def test_el_veredicto_se_niega_sin_una_reconciliacion_acreditada(rm: dict, capsys, monkeypatch) -> None:
    monkeypatch.setattr(rcc, "REPORTS", rm["recon"] / "reports")
    with pytest.raises(SystemExit, match="no reconciliable"):
        rcc.main(["--reconciliation", "--crps-parent-root", str(rm["madre"])])
    _ensamblado(rm, capsys)
    cab._escribir(rm["recon"], rc.REFERENCIA_REL + "/" + VEREDICTO, '{"FAD": {}}\n')
    with pytest.raises(SystemExit, match="capa-cambio"):
        rcc.main(["--reconciliation", "--crps-parent-root", str(rm["madre"])])
    assert not (rm["recon"] / VEREDICTO).exists(), "no se escribe un veredicto que no se acredita"


@pytest.mark.parametrize("args", [["--reconciliation"], ["--reconciliation", "--crps-parent-root", "x", "--promote"],
                                  ["--reconciliation", "--crps-parent-root", "x", "--mlflow"]])  # fmt: skip
def test_el_modo_reconciliacion_no_promueve_ni_corre_sin_madre(args: list[str]) -> None:
    with pytest.raises(SystemExit) as fin:
        rcc.main(args)
    assert fin.value.code == 2


# ═══════════════════════════════ ensamblar: todo se comprueba antes de escribir nada
def _intacto(m: dict, capsys, codigo: str, *fuentes: Path) -> None:
    antes = cab._huellas(m["recon"])
    rc_, out = _ensamblar(m, capsys, *fuentes)
    assert rc_ == 2 and codigo in out["codes"], out
    assert cab._huellas(m["recon"]) == antes and not (m["recon"] / rc.RUTA_REL).exists()


def test_una_fuente_sin_aceptar_no_se_ensambla(rm: dict, capsys, monkeypatch) -> None:
    b2 = _clonar(rm["c5"], rm["tmp"] / "c6b")
    m6 = {"compl": b2, "madre": rm["madre"]}
    c6._calculada(m6, monkeypatch, capsys)
    assert c6._cerrar(m6, capsys)[1]["status"] == "computed"
    monkeypatch.chdir(rm["recon"])
    _intacto(rm, capsys, "fuente-estado", rm["c5"], b2)


def test_dos_fuentes_del_mismo_proposito_no_se_ensamblan(rm: dict, capsys) -> None:
    _intacto(rm, capsys, "fuente-repetida", rm["c5"], rm["c5"], rm["c6"])


def test_una_fuente_que_ya_no_se_acredita_no_se_ensambla(rm: dict, capsys) -> None:
    csv_p = rm["c6"] / cx.CRPS_BASE_REL
    csv_p.write_text(csv_p.read_text() + "\n")
    _intacto(rm, capsys, "fuente-no-acreditada")


def test_una_entrada_congelada_distinta_en_el_arbol_no_se_ensambla(rm: dict, capsys) -> None:
    """El árbol re-valida el CRPS campeón con SU manifiesto: tiene que ser el que C5 congeló."""
    cab._escribir(rm["recon"], cx.MANIFIESTO_CAMPEON_REL, '{"FAD": {"models": ["naive1"], "agg": "median"}}\n')
    _intacto(rm, capsys, "fuente-entrada")


def test_una_vista_redefinida_en_la_duckdb_del_arbol_no_se_ensambla(rm: dict, capsys) -> None:
    mm.redefinir_vista(rm["recon"] / cx.DUCKDB_REL, "mart_series_summary")
    _intacto(rm, capsys, "fuente-duckdb")


def test_otro_panel_en_el_arbol_no_se_ensambla(rm: dict, capsys) -> None:
    cab._escribir(rm["recon"], str(cx.PANEL_REL), mm.panel_csv() + "\n")
    _intacto(rm, capsys, "panel")


def test_una_transaccion_en_el_arbol_impide_ensamblar(rm: dict, capsys) -> None:
    cab._escribir(rm["recon"], str(cx.CAMPANA_REL), "{}\n")
    _intacto(rm, capsys, "transaccion-ajena")


def test_una_evidencia_de_otra_madre_no_se_ensambla(rm: dict, capsys) -> None:
    (rm["recon"] / EVID_DIR / "campaign.json").write_text('{"campaign_id": "otra"}\n')
    mm.escribir_manifiesto(rm["recon"] / EVID_DIR)
    _intacto(rm, capsys, "evidencia-identidad")


def test_un_id_invalido_no_se_ensambla(rm: dict, capsys) -> None:
    antes = cab._huellas(rm["recon"])
    capsys.readouterr()
    args = ["--root", str(rm["recon"]), "assemble", "--parent-root", str(rm["madre"]), "--evidence", EVID,
            "--id", "otro", "--source", str(rm["c5"])]  # fmt: skip
    assert cli.main(args) == 2 and "id" in json.loads(capsys.readouterr().out)["codes"]
    assert cab._huellas(rm["recon"]) == antes


def test_reensamblar_es_otra_reconciliacion(rm: dict, capsys) -> None:
    _ensamblado(rm, capsys)
    rc_, out = _ensamblar(rm, capsys)
    assert rc_ == 2 and out["codes"] == ["ya-ensamblada"], out


def _tar_con(m: dict, escribir) -> None:
    carpeta = m["recon"] / EVID_DIR
    with tarfile.open(carpeta / mm.ARCHIVO, "w:gz") as tar:
        escribir(tar)
    mm.escribir_manifiesto(carpeta)


def test_un_miembro_repetido_en_el_tar_no_se_ensambla(rm: dict, capsys) -> None:
    def dos_veces(tar: tarfile.TarFile) -> None:
        for datos in (b"a\n", b"b\n"):
            mm._miembro(tar, POOL, datos)

    _tar_con(rm, dos_veces)
    _intacto(rm, capsys, "madre-duplicado")


def test_un_enlace_en_el_tar_no_se_ensambla(rm: dict, capsys) -> None:
    def enlace(tar: tarfile.TarFile) -> None:
        info = tarfile.TarInfo(POOL)
        info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tar.addfile(info)

    _tar_con(rm, enlace)
    _intacto(rm, capsys, "madre-no-regular")


# ═══════════════════════════════ C4 (E3): la fuente se re-puntúa con su puntuador real
@pytest.fixture
def con_e3(rm: dict, monkeypatch, capsys) -> dict:
    """Un clon C con C4 `accredited` (lanes dobles; orquestador, acreditador y puntuador reales)."""
    c = _clonar(rm["c5"], rm["tmp"] / "c4")
    m4 = {"compl": c, "madre": rm["madre"]}
    cab._en_curso(m4, "e3", capsys)
    cab._lanes_dobles(m4, monkeypatch)
    assert e3.main(["--complement"]) == 0
    assert cab._cli(m4, "close", "--parent-root", str(rm["madre"]), capsys=capsys)[1]["status"] == "computed"
    (c / rc.E3_REL).write_text(
        json.dumps(score.puntuar(root=c, complemento=rm["madre"]), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )
    assert cab._cli(m4, "accept", "--parent-root", str(rm["madre"]), capsys=capsys)[1]["status"] == "accredited"
    monkeypatch.chdir(rm["recon"])
    return {**rm, "c4": c}


def test_control_e3_se_ensambla_con_su_informe_y_sus_salidas(con_e3: dict, capsys) -> None:
    rc_, out = _ensamblar(con_e3, capsys, con_e3["c5"], con_e3["c6"], con_e3["c4"])
    assert rc_ == 0, out
    arts = out["sources"][cab.ID_E3]
    assert rc.E3_REL in arts and sum(a.startswith(cx.LANES_DIR) for a in arts) == 4
    for a in arts:
        assert _sha(con_e3["recon"] / a) == _sha(con_e3["c4"] / a)
    assert cli.main(["--root", str(con_e3["recon"]), "verify", "--parent-root", str(con_e3["madre"])]) == 0


def test_un_informe_e3_que_sus_recibos_no_dan_no_se_ensambla(con_e3: dict, capsys) -> None:
    informe = json.loads((con_e3["c4"] / rc.E3_REL).read_text())
    informe["n_lanes"] = 99
    (con_e3["c4"] / rc.E3_REL).write_text(json.dumps(informe))
    _intacto(con_e3, capsys, "fuente-e3", con_e3["c5"], con_e3["c4"])


def test_e3_sin_sus_lectores_no_se_acredita(con_e3: dict, capsys) -> None:
    assert _ensamblar(con_e3, capsys, con_e3["c5"], con_e3["c4"])[0] == 0
    assert "fuente-lector" in _codigos(con_e3)
    assert "fuente-lector" in _codigos(con_e3, acreditar_lanes=score.acreditar_recibos), "sin puntuador tampoco"
    assert _codigos(con_e3, solo=c5.ID_C5) == set(), "un consumidor de CRPS no necesita re-puntuar E3"


def test_e3_informe_tocado_despues_de_ensamblar_no_se_acredita(con_e3: dict, capsys) -> None:
    assert _ensamblar(con_e3, capsys, con_e3["c5"], con_e3["c4"])[0] == 0
    informe = json.loads((con_e3["c4"] / rc.E3_REL).read_text())
    informe["n_lanes"] = 5
    (con_e3["c4"] / rc.E3_REL).write_text(json.dumps(informe))
    assert cli.main(["--root", str(con_e3["recon"]), "verify", "--parent-root", str(con_e3["madre"])]) == 2


# ═══════════════════════════════ artefactos por propósito
def test_cada_proposito_declara_sus_artefactos(tmp_path: Path) -> None:
    assert rc.artefactos_de(tmp_path, {"purpose": "deep_pi_conformal"}) == [cx.CONFORMAL_REL, cx.GAMMA_REL,
                                                                            cx.PROCEDENCIA_REL]  # fmt: skip
    assert rc.artefactos_de(tmp_path, {"purpose": "baseline_crps"}) == [cx.CRPS_BASE_REL,
                                                                        f"{cx.CRPS_BASE_REL}.receipt.json"]  # fmt: skip
    with pytest.raises(rc.ReconciliacionError):
        rc.artefactos_de(tmp_path, {"purpose": "otro"})
    assert all(rc.es_salida(s) for s in rc.SALIDAS) and rc.es_salida(f"{rc.FIGURAS}x.pdf")
    assert not rc.es_salida(cx.CRPS_REL)


# ═══════════════════════════════ guardas aisladas (ronda de mutación)
def test_lo_que_el_tar_trae_fuera_de_reports_se_excluye_y_no_se_escribe(rm: dict, capsys) -> None:
    mm.evidencia_madre(rm["recon"] / EVID_DIR, rm["madre"], _miembros_madre(rm["madre"]),
                       extra={"models.dvc": b"outs: []\n"})  # fmt: skip
    _ensamblado(rm, capsys)
    doc = rc.leer(rm["recon"])
    assert {"member": "models.dvc", "reason": "fuera de reports/: no es una salida de datos"} in doc["excluded"]
    assert not (rm["recon"] / "models.dvc").exists() and "models.dvc" not in doc["mother_layers"]


def test_solo_una_fuente_que_no_esta_no_se_acredita(rm: dict, capsys) -> None:
    _ensamblado(rm, capsys)
    assert _codigos(rm, solo="compl_otro") == {"fuente-ausente"}


def _plan_resellado_con(m: dict, **campos) -> None:
    _resellar_plan(m["recon"], lambda d: d.update(campos))


@pytest.mark.parametrize(("campos", "codigo"), [({"schema": "campaign-reconciliation/0"}, "esquema")])
def test_un_plan_resellado_con_otro_esquema_no_se_lee(rm: dict, capsys, campos: dict, codigo: str) -> None:
    _ensamblado(rm, capsys)
    _plan_resellado_con(rm, **campos)
    assert _codigos(rm) == {codigo}


def test_un_estado_desconocido_no_se_lee(rm: dict, capsys) -> None:
    _ensamblado(rm, capsys)
    ruta = rm["recon"] / rc.RUTA_REL
    ruta.write_text(ruta.read_text().replace('"status": "assembled"', '"status": "validated"'))
    assert _codigos(rm) == {"estado"}


def test_fallar_es_terminal(rm: dict, capsys) -> None:
    _ensamblado(rm, capsys)
    assert cli.main(["--root", str(rm["recon"]), "fail", "--reason", "prueba"]) == 0
    with pytest.raises(rc.ReconciliacionError) as fallo:
        rc.fallar(rm["recon"], "otra vez")
    assert fallo.value.codigos == {"transicion"}
    with pytest.raises(rc.ReconciliacionError) as cierre:
        rc.cerrar(rm["recon"], parent_root=rm["madre"])
    assert cierre.value.codigos == {"estado"}


def test_el_veredicto_exige_una_fuente_de_crps_campeon(rm: dict, capsys, monkeypatch) -> None:
    assert _ensamblar(rm, capsys, rm["c6"])[0] == 0
    monkeypatch.setattr(rcc, "REPORTS", rm["recon"] / "reports")
    with pytest.raises(SystemExit, match="fuente-ausente"):
        rcc.main(["--reconciliation", "--crps-parent-root", str(rm["madre"])])


def test_una_fuente_que_no_declara_el_csv_no_responde_por_el(rm: dict, capsys, monkeypatch) -> None:
    _ensamblado(rm, capsys)
    leer = rc.leer

    def sin_csv(root):
        doc = leer(root)
        for s in doc["sources"]:
            s["artifacts"].pop(cx.CRPS_REL, None)
        return doc

    monkeypatch.setattr(rc, "leer", sin_csv)
    p = _crps(rm)
    assert p["status"] == "heredado" and "no responde por un CRPS" in p["reason"], p


@pytest.mark.parametrize(("ruta", "codigo"), [("reports/governance/key_facts.json", "fuente-ruta"),
                                              ("data/processed/visa_panel_long.csv", "fuente-ruta"),
                                              ("reports/../experiments/codigo.py", "fuente-ruta"),
                                              ("reports/eval/no_existe.csv", "fuente-artefacto")])  # fmt: skip
def test_una_fuente_solo_aporta_datos_que_existen_y_no_son_salidas(
    rm: dict, capsys, monkeypatch, ruta: str, codigo: str
) -> None:
    artefactos = rc.artefactos_de
    monkeypatch.setattr(rc, "artefactos_de", lambda src, doc: [*artefactos(src, doc), ruta])
    _intacto(rm, capsys, codigo)


def test_la_coherencia_de_una_fuente_con_la_madre_y_el_arbol(rm: dict) -> None:
    padre = cx.leer_padre(rm["madre"])
    doc = cx.leer(rm["c6"])
    sello = {"complement_id": "c", "duckdb_catalog_sha256": "x", "frozen_reports": {}}
    assert rc._coherencia_fuente(sello, doc, padre, rm["recon"], "x") == []
    otra = {**doc, "parent": {**doc["parent"], "campaign_json_sha256": "0" * 64}, "panel_sha256": "sha256:0"}
    codigos = {c for c, _ in rc._coherencia_fuente(sello, otra, padre, rm["recon"], "y")}
    assert codigos == {"fuente-otra-madre", "fuente-panel", "fuente-duckdb"}
    otra_madre = {**doc, "parent": {**doc["parent"], "campaign_id": "rederiv_otra"}}
    assert {c for c, _ in rc._coherencia_fuente(sello, otra_madre, padre, rm["recon"], "x")} == {"fuente-otra-madre"}


def test_un_tar_ilegible_no_se_ensambla(rm: dict, capsys) -> None:
    carpeta = rm["recon"] / EVID_DIR
    (carpeta / mm.ARCHIVO).write_bytes(b"esto no es un tar.gz")
    mm.escribir_manifiesto(carpeta)
    _intacto(rm, capsys, "evidencia-archivo")


def test_un_plan_que_da_dos_autoridades_a_una_ruta_no_se_acredita(rm: dict, capsys) -> None:
    _ensamblado(rm, capsys)
    sha = _sha(rm["recon"] / cx.CRPS_BASE_REL)
    _resellar_plan(rm["recon"], lambda d: d["mother_layers"].update({cx.CRPS_BASE_REL: sha}))
    assert "plan-solapado" in _codigos(rm)


def test_dos_fuentes_que_reclaman_la_misma_ruta_no_se_ensamblan(rm: dict, capsys, monkeypatch) -> None:
    artefactos = rc.artefactos_de
    comun = "reports/eval/series_cohorts.json"
    monkeypatch.setattr(rc, "artefactos_de", lambda src, doc: [*artefactos(src, doc), comun])
    _intacto(rm, capsys, "fuente-solapada")


def test_una_duckdb_de_otro_panel_no_se_ensambla(rm: dict, capsys) -> None:
    otro = rm["tmp"] / "otro_panel.csv"
    otro.write_text(mm.panel_csv() + "\n")
    (rm["recon"] / cx.DUCKDB_REL).unlink()
    mm.construir_duckdb(rm["recon"] / cx.DUCKDB_REL, otro, rm["tmp"] / "build_otro")
    _intacto(rm, capsys, "duckdb-panel")


def test_fallar_con_un_estado_desconocido_es_un_error_del_contrato(rm: dict, capsys) -> None:
    _ensamblado(rm, capsys)
    ruta = rm["recon"] / rc.RUTA_REL
    ruta.write_text(ruta.read_text().replace('"status": "assembled"', '"status": "rara"'))
    with pytest.raises(rc.ReconciliacionError) as fallo:
        rc.fallar(rm["recon"], "prueba")
    assert fallo.value.codigos == {"estado"}
