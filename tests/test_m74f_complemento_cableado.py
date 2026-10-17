"""M74-F · C3/C4 — cableado de la transacción complementaria en los productores y el CLI.

Integración con las APIs REALES: el contrato (`vp_model.complement`), el acreditador y el puntuador de
E3, el orquestador `run_e3_campaign`, `improve_conformal`, la identidad de lane de `hpo_winner_receipt`
y el CLI `run_complement`. El git también es real, en un repositorio temporal: el git inyectado de
las pruebas del contrato escondía que `reports/` está versionado y que `complement.json` ensucia el
árbol, así que `iniciar` habría caído siempre en ``codigo-sucio``.

Sólo los PRODUCTORES son dobles: los lanes (el subprocess de cada runner), `_evaluate` de conformal y
la carga de series. Todo vive en directorios temporales; ninguna prueba toca `reports/` vivo ni entrena.
El repositorio temporal lleva el `.gitignore` REAL de este checkout (las reglas que deciden qué es sucio),
una DuckDB temporal construida con el builder real y la evidencia de la madre con la forma de la real.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from tests import campana_valida
from tests import m74f_mundo as mm

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "experiments"))

pytest.importorskip("darts")

import hpo_winner_receipt as hwr  # noqa: E402
import improve_conformal as ic  # noqa: E402
import pandas as pd  # noqa: E402
import run_complement as cli  # noqa: E402
import run_e3_campaign as e3  # noqa: E402
import score_e3_campaign as score  # noqa: E402

from vp_model import complement as cx  # noqa: E402

MADRE = "rederiv_953cd00_20261006T073927"
#: El SHA de la madre empieza por el de su id, como exige el manifiesto de campaña (`campaign_manifest`).
SHA_MADRE = "953cd00" + "9" * 33
ID_E3 = "compl_m74f_e3_t1"
ID_C3 = "compl_m74f_c3_t1"
EVID_DIR = "reports/campaign/complement_evidence"
EVID = f"{EVID_DIR}/SHA256SUMS"
PANEL = mm.panel_csv()
#: La función real, antes de que el parche autouse la mida contra RAIZ.
CODIGO_CARGADO = cx.codigo_cargado
PAISES = ("mexico", "india", "china", "philippines", "all", "row", "vietnam")


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _huellas(raiz: Path) -> dict[Path, str]:
    return {p: _sha(p) for p in raiz.rglob("*") if p.is_file() and ".git" not in p.parts}


def _git(raiz: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}  # fmt: skip
    hooks = raiz.parent / "sin_hooks"
    hooks.mkdir(exist_ok=True)
    fin = subprocess.run(["git", "-c", f"core.hooksPath={hooks}", "-c", "commit.gpgsign=false", *args],
                         cwd=raiz, env=env, capture_output=True, text=True, check=True)  # fmt: skip
    return fin.stdout


def _escribir(raiz: Path, rel: str, texto: str) -> Path:
    p = raiz / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(texto)
    return p


def _scorecard(tabla: str, desplazamiento: float) -> str:
    filas = "".join(f"naive1,{p},F1,{tabla},{1.0 + desplazamiento + i / 10:.3f}\n" for i, p in enumerate(PAISES))
    return "model,country,category,table,hold_mase\n" + filas


def _deep_pi(tabla: str) -> str:
    filas = "".join(f"{p}/F/F1,2025-0{m}-01,1.0,0.0,2.0\n" for p in PAISES for m in (1, 2, 3))
    return "unique_id,ds,BiTCN,BiTCN-lo-95,BiTCN-hi-95\n" + filas


@pytest.fixture(autouse=True)
def _codigo_del_checkout(monkeypatch) -> None:
    """El código que corre en estas pruebas sale de ESTE checkout, no del repo temporal: `codigo_cargado`
    se mide contra RAIZ. Las pruebas `codigo-ajeno` restauran la función real y la ven fallar."""
    monkeypatch.setattr(cx, "codigo_cargado", lambda root, modulos=None: CODIGO_CARGADO(RAIZ, modulos))


@pytest.fixture
def mundo(tmp_path: Path, monkeypatch, campana_sellada) -> dict:
    """Madre `computed` con lo que re-derivó; complementaria = repo git real en el commit de M74-F."""
    monkeypatch.delenv("CAMPAIGN_ID", raising=False)
    monkeypatch.delenv("CAMPAIGN_SHA", raising=False)
    madre, compl = tmp_path / "madre", tmp_path / "compl"
    for r in (madre, compl):
        _escribir(r, "data/processed/visa_panel_long.csv", PANEL)
    sello = "sha256:" + _sha(madre / "data/processed/visa_panel_long.csv")
    # G7 · B5a: la madre lleva el ESQUEMA REAL de la máquina de estados (no una identidad de juguete de cinco
    # claves): así las pruebas pueden recorrer `computed → validated → published` sobre ella.
    # …y con su manifiesto de campaña y su sello legítimos (el fabricante de las pruebas de publicación): sin
    # ellos la madre no podría pasar nunca la puerta, y la prueba de publicar no mediría nada.
    sello_madre = campana_sellada.identidad(campana_sellada(madre, campaign_id=MADRE, head=SHA_MADRE))
    _escribir(madre, "reports/campaign/campaign.json", json.dumps(
        {**campana_valida.transaccion(campaign_id=MADRE, source_git_sha=SHA_MADRE, panel_sha256=sello,
                                      status="computed"), "input_seal_sha256": sello_madre["input_seal_sha256"]},
        indent=2, sort_keys=True) + "\n")  # fmt: skip
    _escribir(madre, "reports/eval/cohort_scan.json", '{"rederivado": "8c"}\n')
    for n in score.SCORECARDS:
        _escribir(madre, f"reports/eval/{n}", _scorecard(n.removeprefix("model_comparison_")[:3], 0.0))
    for t in ("FAD", "DFF"):
        _escribir(madre, cx.artefacto_c3("baseline", t)[0], _deep_pi(t))
        _escribir(madre, cx.artefacto_c3("forecasts", t)[0], f"unique_id,ds,BiTCN\nmexico/F/F1,2025-01-01,{t}\n")

    # complementaria: el árbol de código tal como lo deja un checkout limpio
    lock = _escribir(compl, "locks/deep.txt", "torch==2.13.0\n")
    cohortes = _escribir(compl, "reports/eval/series_cohorts.json", json.dumps({"rule_version": "r1", "series": [
        {"country": p, "category": "F1", "table": t, "cohort": "all"} for p in PAISES for t in ("FAD", "DFF")]}))  # fmt: skip
    _escribir(compl, "reports/eval/cohort_scan.json", '{"declarado": "antes de la madre"}\n')
    for n in score.SCORECARDS:
        _escribir(compl, f"reports/eval/{n}", _scorecard(n.removeprefix("model_comparison_")[:3], 0.5))
    _escribir(compl, "reports/campaign/e3/receipt_viejo_FAD_all.json", '{"campaign": null}\n')
    _escribir(compl, "experiments/codigo.py", "X = 1\n")
    _escribir(compl, "docs/cohort_deck.json", json.dumps({
        "version": "t1", "universe": "evaluable", "cohorts": ["all"], "tables": ["FAD", "DFF"], "seeds": [1],
        "device": "cpu", "frozen": {"cv": {"val_size": 12}}, "max_primary_per_cohort": 1,
        "recipes": {
            "primary-a": {"role": "primary", "model": "BiTCN", "params": {}, "space": "diff"},
            "control-gbm-lightgbm": {"role": "control", "model": "lightgbm",
                                     "params": {"runner": "run_global_gbm.py"}, "space": "levels"}},
        "inputs": {"visa_panel_long.csv": _sha(compl / "data/processed/visa_panel_long.csv"),
                   "locks/deep.txt": _sha(lock), "cohort_scan.json": "806b3fed" + "0" * 56,
                   "series_cohorts.json": _sha(cohortes), "commit": "4f02d2e"}}))  # fmt: skip
    (compl / ".gitignore").write_bytes((RAIZ / ".gitignore").read_bytes())
    mm.construir_duckdb(compl / cx.DUCKDB_REL, compl / cx.PANEL_REL, tmp_path / "build")
    _git(compl, "init", "-q")
    _git(compl, "add", "-A")
    _git(compl, "commit", "-qm", "m74f")
    return {"madre": madre, "compl": compl, "huellas_madre": _huellas(madre)}


def _cli(m: dict, *args: str, capsys) -> tuple[int, dict]:
    capsys.readouterr()
    rc = cli.main(["--root", str(m["compl"]), *args])
    return rc, json.loads(capsys.readouterr().out)


def _planificada(m: dict, purpose: str, capsys, cid: str = ID_E3) -> None:
    assert _cli(m, "import", "--parent-root", str(m["madre"]), "--purpose", purpose, capsys=capsys)[0] == 0
    _evidencia(m, purpose)
    rc, out = _cli(m, "plan", "--parent-root", str(m["madre"]), "--id", cid, "--purpose", purpose,
                   "--evidence", EVID, capsys=capsys)  # fmt: skip
    assert rc == 0 and out["status"] == "planned", out


def _evidencia(m: dict, purpose: str, *, sin: str | None = None) -> None:
    """La evidencia de la madre copiada bajo reports/: su campaign.json, el tar con lo importado y SHA256SUMS."""
    artefactos = [d["artifact"] for d in cli.importaciones(purpose) if d["artifact"] != sin]
    mm.evidencia_madre(m["compl"] / EVID_DIR, m["madre"], artefactos)


def _en_curso(m: dict, purpose: str, capsys, cid: str = ID_E3) -> None:
    _planificada(m, purpose, capsys, cid)
    rc, out = _cli(m, "start", "--parent-root", str(m["madre"]), capsys=capsys)
    assert rc == 0 and out["status"] == "running", out


def _arg(cmd: list[str], flag: str) -> str:
    return cmd[cmd.index(flag) + 1]


def _lanes_dobles(
    m: dict, monkeypatch, *, identidad=None, ignorar_receipt: bool = False, despues=None, entornos=None
) -> list[list[str]]:
    """Sustituye el subprocess de cada runner: escribe la salida y el recibo con la MISMA forma y la
    identidad que el runner real pide a `lane_campaign_identity`, bajo el entorno que le pasa el orquestador.
    ``despues(root)`` corre al terminar cada lane (p. ej. alguien edita el código entre lanes)."""
    root, llamadas = m["compl"], []

    def correr(cmd, cwd, check, env):
        llamadas.append(cmd)
        if entornos is not None:
            entornos.append(dict(env))
        recipe, table, cohort = _arg(cmd, "--recipe"), _arg(cmd, "--table"), _arg(cmd, "--cohort")
        salida = _escribir(root, f"reports/eval/e3_{recipe}_{table}_{cohort}.csv", "country,category,hold_mase\n"
                           + "".join(f"{p},F1,{0.8 + i / 10:.3f}\n" for i, p in enumerate(PAISES)))  # fmt: skip
        with mock.patch.dict(os.environ, env, clear=True):
            campana = identidad if identidad is not None else hwr.lane_campaign_identity(root)
        recibo = {"recipe": recipe, "recipe_role": "control" if "gbm" in recipe else "primary",
                  "recipe_model": "x", "recipe_space": "levels", "table": table, "cohort": cohort,
                  "seed": int(_arg(cmd, "--seed")), "campaign": campana, "status": "ok", "exception": None,
                  "timing": {"seconds": 1.0}, "n_series": len(PAISES),
                  "outputs": [{"path": str(salida.relative_to(root)), "sha256": _sha(salida)}]}  # fmt: skip
        destino = (
            f"reports/campaign/e3/{Path(_arg(cmd, '--receipt')).name}" if ignorar_receipt else _arg(cmd, "--receipt")
        )
        _escribir(root, destino, json.dumps(recibo))
        if despues is not None:
            despues(root)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(e3, "ROOT", root)
    monkeypatch.setattr(e3, "subprocess", SimpleNamespace(run=correr))  # sólo el del orquestador
    return llamadas


# ═══════════════════════════════ C4 · ciclo completo con las APIs reales
def test_c4_ciclo_completo_cli_orquestador_y_puntuador(mundo: dict, monkeypatch, capsys) -> None:
    viejo = mundo["compl"] / "reports/campaign/e3/receipt_viejo_FAD_all.json"
    antes_viejo = _sha(viejo)
    _en_curso(mundo, "e3", capsys)
    llamadas = _lanes_dobles(mundo, monkeypatch)
    assert e3.main(["--complement"]) == 0
    assert len(llamadas) == 4 and all(_arg(c, "--receipt").startswith(cx.LANES_DIR) for c in llamadas)
    assert _cli(mundo, "close", "--parent-root", str(mundo["madre"]), capsys=capsys)[1]["status"] == "computed"

    informe = score.puntuar(root=mundo["compl"], complemento=mundo["madre"])
    assert informe["campaign"]["campaign_id"] == ID_E3 and informe["identity_source"] == "complement"
    assert informe["complement"]["parent"] == MADRE and informe["n_lanes"] == 4
    assert {i for i in informe["complement"]["imports"]} >= {"reports/eval/cohort_scan.json"}

    assert _cli(mundo, "accept", "--parent-root", str(mundo["madre"]), capsys=capsys)[1]["status"] == "accredited"
    rc, acta = _cli(mundo, "verify", "--parent-root", str(mundo["madre"]), capsys=capsys)
    assert rc == 0 and acta["identity"]["campaign_id"] == ID_E3 and acta["lanes"] == 4
    assert _huellas(mundo["madre"]) == mundo["huellas_madre"], "la madre sólo se lee"
    assert _sha(viejo) == antes_viejo, "los recibos versionados de otra corrida no se tocan ni se leen"


def test_el_puntuador_usa_la_referencia_naive1_importada_de_la_madre(mundo: dict, monkeypatch, capsys) -> None:
    _en_curso(mundo, "e3", capsys)
    _lanes_dobles(mundo, monkeypatch)
    e3.main(["--complement"])
    _cli(mundo, "close", "--parent-root", str(mundo["madre"]), capsys=capsys)
    ref = score.naive1_por_serie(mundo["compl"])
    assert ref[("mexico", "F1", "FAD")] == pytest.approx(1.0), "la del árbol era 1.5: no es la de la madre"


# ═══════════════════════════════ git real · código limpio, datos sellados
def test_reports_versionado_no_cuenta_como_codigo_sucio(mundo: dict) -> None:
    head = _git(mundo["compl"], "rev-parse", "HEAD").strip()
    _escribir(mundo["compl"], "reports/eval/cohort_scan.json", "otro\n")
    _escribir(mundo["compl"], "reports/campaign/complement.json", "{}\n")
    assert cx.estado_git(mundo["compl"]) == (head, False)
    _escribir(mundo["compl"], "experiments/codigo.py", "X = 2\n")
    assert cx.estado_git(mundo["compl"]) == (head, True)


@pytest.mark.parametrize("rel", ["experiments/nuevo.py", "data/processed/extra.csv", "docs/cohort_deck.json"])
def test_codigo_o_datos_fuera_de_reports_sin_commitear_impiden_iniciar(mundo: dict, capsys, rel: str) -> None:
    _planificada(mundo, "e3", capsys)
    p = mundo["compl"] / rel
    _escribir(mundo["compl"], rel, (p.read_text() if p.exists() else "") + " ")
    rc, out = _cli(mundo, "start", "--parent-root", str(mundo["madre"]), capsys=capsys)
    assert rc == 2 and "codigo-sucio" in out["codes"]
    assert cx.leer(mundo["compl"])["status"] == "failed"


def test_un_commit_a_mitad_impide_cerrar(mundo: dict, monkeypatch, capsys) -> None:
    _en_curso(mundo, "e3", capsys)
    _lanes_dobles(mundo, monkeypatch)
    e3.main(["--complement"])
    _escribir(mundo["compl"], "experiments/codigo.py", "X = 3\n")
    _git(mundo["compl"], "commit", "-qam", "a mitad")
    rc, out = _cli(mundo, "close", "--parent-root", str(mundo["madre"]), capsys=capsys)
    assert rc == 2 and "codigo" in out["codes"]


# ═══════════════════════════════ identidad del lane
def test_identidad_del_lane_segun_el_estado_de_la_complementaria(mundo: dict, monkeypatch, capsys) -> None:
    assert hwr.lane_campaign_identity(mundo["compl"]) is None, "sin transacción no se inventa"
    _planificada(mundo, "e3", capsys)
    assert hwr.lane_campaign_identity(mundo["compl"]) is None, "planificada no es en curso"
    _cli(mundo, "start", "--parent-root", str(mundo["madre"]), capsys=capsys)
    ident = hwr.lane_campaign_identity(mundo["compl"])
    assert ident == cx.identidad(cx.leer(mundo["compl"])) and ident["campaign_id"] == ID_E3
    monkeypatch.setenv("CAMPAIGN_ID", MADRE)
    with pytest.raises(hwr.WinnerReceiptError, match="otra corrida"):
        hwr.lane_campaign_identity(mundo["compl"])


def test_campana_y_complementaria_en_curso_a_la_vez_no_tienen_identidad(mundo: dict, capsys) -> None:
    _en_curso(mundo, "e3", capsys)
    _escribir(mundo["compl"], "reports/campaign/campaign.json", json.dumps(
        {"campaign_id": "rederiv_x", "status": "running", "source_git_sha": "1" * 40, "panel_sha256": "sha256:x"}))  # fmt: skip
    with pytest.raises(hwr.WinnerReceiptError, match="a la vez"):
        hwr.lane_campaign_identity(mundo["compl"])


def test_plan_editado_o_complementaria_de_c3_detienen_al_lane(mundo: dict, capsys) -> None:
    _en_curso(mundo, "deep_pi_conformal", capsys, cid=ID_C3)
    with pytest.raises(hwr.WinnerReceiptError, match="ajena a E3"):
        hwr.lane_campaign_identity(mundo["compl"])
    ruta = mundo["compl"] / cx.RUTA_REL
    doc = json.loads(ruta.read_text())
    ruta.write_text(json.dumps({**doc, "purpose": "e3"}))
    with pytest.raises(hwr.WinnerReceiptError, match="plan-editado"):
        hwr.lane_campaign_identity(mundo["compl"])


# ═══════════════════════════════ orquestador E3
def test_sin_bandera_el_orquestador_se_niega_en_un_arbol_con_complementaria(mundo, monkeypatch, capsys) -> None:
    _en_curso(mundo, "e3", capsys)
    llamadas = _lanes_dobles(mundo, monkeypatch)
    with pytest.raises(SystemExit, match="--complement"):
        e3.main([])
    assert llamadas == []


def test_el_orquestador_no_lanza_lanes_fuera_de_running(mundo: dict, monkeypatch, capsys) -> None:
    _planificada(mundo, "e3", capsys)
    llamadas = _lanes_dobles(mundo, monkeypatch)
    with pytest.raises(SystemExit, match="no-en-curso"):
        e3.main(["--complement"])
    assert llamadas == []


def test_baraja_cambiada_despues_del_plan_no_lanza_lanes(mundo: dict, monkeypatch, capsys) -> None:
    """Con git real la baraja es código: editarla ensucia el árbol y la guarda de código la para antes que
    `plan_de_baraja` (que sigue cubriendo una baraja pasada por parámetro al puntuador)."""
    _en_curso(mundo, "e3", capsys)
    deck = mundo["compl"] / "docs/cohort_deck.json"
    crudo = json.loads(deck.read_text())
    deck.write_text(json.dumps({**crudo, "cohorts": ["all", "estable"]}))
    llamadas = _lanes_dobles(mundo, monkeypatch)
    with pytest.raises(SystemExit, match="codigo-sucio"):
        e3.main(["--complement"])
    assert llamadas == []


def test_campana_completa_concurrente_bloquea_el_orquestador(mundo: dict, monkeypatch, capsys) -> None:
    _en_curso(mundo, "e3", capsys)
    _escribir(mundo["compl"], "reports/campaign/campaign.json", '{"status": "running"}')
    llamadas = _lanes_dobles(mundo, monkeypatch)
    with pytest.raises(SystemExit, match="campana-concurrente"):
        e3.main(["--complement"])
    assert llamadas == []


@pytest.mark.parametrize(
    "caso",
    ["identidad_de_la_madre", "recibos_en_el_directorio_de_la_campana"],
)
def test_lanes_mal_escritos_no_cierran_y_no_se_puntuan(mundo: dict, monkeypatch, capsys, caso: str) -> None:
    _en_curso(mundo, "e3", capsys)
    if caso == "identidad_de_la_madre":
        _lanes_dobles(mundo, monkeypatch, identidad={"campaign_id": MADRE, "source_git_sha": SHA_MADRE,
                                                     "panel_sha256": cx.leer(mundo["compl"])["panel_sha256"]})  # fmt: skip
    else:
        _lanes_dobles(mundo, monkeypatch, ignorar_receipt=True)
    e3.main(["--complement"])
    rc, out = _cli(mundo, "close", "--parent-root", str(mundo["madre"]), capsys=capsys)
    assert rc == 2 and out["codes"] == ["lanes"]
    with pytest.raises(score.E3NoAcreditado):
        score.puntuar(root=mundo["compl"], complemento=mundo["madre"])


def test_un_campaign_id_heredado_de_la_shell_no_contamina_los_lanes(mundo: dict, monkeypatch, capsys) -> None:
    """El operador viene de la madre con su CAMPAIGN_ID exportado: el orquestador lo sustituye por el de la complementaria."""
    _en_curso(mundo, "e3", capsys)
    monkeypatch.setenv("CAMPAIGN_ID", MADRE)
    monkeypatch.setenv("CAMPAIGN_SHA", SHA_MADRE)
    _lanes_dobles(mundo, monkeypatch)
    assert e3.main(["--complement"]) == 0
    recibos = sorted((mundo["compl"] / cx.LANES_DIR).glob("receipt_*.json"))
    assert len(recibos) == 4 and {json.loads(r.read_text())["campaign"]["campaign_id"] for r in recibos} == {ID_E3}


# ═══════════════════════════════ puntuador E3
def test_el_puntuador_exige_que_la_baraja_sea_el_plan(mundo: dict, monkeypatch, capsys) -> None:
    """Con git real una baraja editada ya es código sucio; esta guarda cubre una baraja pasada por parámetro."""
    from dataclasses import replace

    from vp_model.deck import cargar_deck

    _en_curso(mundo, "e3", capsys)
    _lanes_dobles(mundo, monkeypatch)
    e3.main(["--complement"])
    _cli(mundo, "close", "--parent-root", str(mundo["madre"]), capsys=capsys)
    otra = replace(cargar_deck(mundo["compl"] / "docs/cohort_deck.json"), cohorts=("all", "estable"))
    with pytest.raises(score.E3NoAcreditado, match="plan-baraja"):
        score.puntuar(root=mundo["compl"], complemento=mundo["madre"], deck=otra)


def test_el_puntuador_no_acredita_una_complementaria_en_curso(mundo: dict, monkeypatch, capsys) -> None:
    _en_curso(mundo, "e3", capsys)
    _lanes_dobles(mundo, monkeypatch)
    e3.main(["--complement"])
    with pytest.raises(score.E3NoAcreditado, match="'running' no se consume"):
        score.puntuar(root=mundo["compl"], complemento=mundo["madre"])


def test_el_puntuador_rechaza_una_complementaria_de_c3(mundo: dict, capsys) -> None:
    _en_curso(mundo, "deep_pi_conformal", capsys, cid=ID_C3)
    with pytest.raises(score.E3NoAcreditado, match="proposito"):
        score.puntuar(root=mundo["compl"], complemento=mundo["madre"])


def test_el_puntuador_sin_bandera_sigue_exigiendo_campana_en_curso(mundo: dict, capsys) -> None:
    _en_curso(mundo, "e3", capsys)
    with pytest.raises(score.E3NoAcreditado, match="sin campaña en curso"):
        score.puntuar(root=mundo["compl"])


def test_main_del_puntuador_exige_las_dos_banderas_juntas() -> None:
    with pytest.raises(SystemExit):
        score.main(["--complement"])


# ═══════════════════════════════ CLI
def test_las_importaciones_no_se_tocan_despues_del_plan(mundo: dict, capsys) -> None:
    _planificada(mundo, "e3", capsys)
    rc, out = _cli(mundo, "import", "--parent-root", str(mundo["madre"]), "--purpose", "e3", capsys=capsys)
    assert rc == 2 and out["codes"] == ["ya-planificada"]


def test_de_una_madre_publicada_no_se_importa_nada(mundo: dict, capsys) -> None:
    """G7 · B5a: publicar consume el permiso; una complementaria nueva no puede empezar sobre lo ya servido."""
    ruta = mundo["madre"] / cx.CAMPANA_REL
    ruta.write_text(json.dumps({**json.loads(ruta.read_text()), "status": "published"}))
    antes = sorted(str(f.relative_to(mundo["compl"])) for f in mundo["compl"].rglob("*") if f.is_file())
    rc, out = _cli(mundo, "import", "--parent-root", str(mundo["madre"]), "--purpose", "e3", capsys=capsys)
    assert rc == 2 and out["codes"] == ["padre-estado"], out
    assert sorted(str(f.relative_to(mundo["compl"])) for f in mundo["compl"].rglob("*") if f.is_file()) == antes


def test_plan_con_evidencia_inexistente_no_escribe_nada(mundo: dict, capsys) -> None:
    _cli(mundo, "import", "--parent-root", str(mundo["madre"]), "--purpose", "e3", capsys=capsys)
    rc, out = _cli(mundo, "plan", "--parent-root", str(mundo["madre"]), "--id", ID_E3, "--purpose", "e3",
                   "--evidence", "reports/campaign/complement_evidence/no_existe.txt", capsys=capsys)  # fmt: skip
    assert rc == 2 and set(out["codes"]) == {"evidencia-manifiesto"}
    assert not (mundo["compl"] / cx.RUTA_REL).exists()


def test_plan_de_c4_sin_importar_no_planifica(mundo: dict, capsys) -> None:
    _evidencia(mundo, "e3")
    rc, out = _cli(mundo, "plan", "--parent-root", str(mundo["madre"]), "--id", ID_E3, "--purpose", "e3",
                   "--evidence", EVID, capsys=capsys)  # fmt: skip
    assert rc == 2 and "importacion-sustituida" in out["codes"], "la copia del árbol no es la de la madre"


def test_el_plan_de_c4_sella_lanes_semilla_entradas_e_importaciones(mundo: dict, capsys) -> None:
    _planificada(mundo, "e3", capsys)
    doc = cx.leer(mundo["compl"])
    assert len(doc["lanes"]) == 4 and doc["seed"] == 1 and doc["lanes_dir"] == cx.LANES_DIR
    assert set(doc["inputs"]) == {"data/processed/visa_panel_long.csv", "locks/deep.txt",
                                  "reports/eval/series_cohorts.json"}, "cohort_scan se importa, no se congela"  # fmt: skip
    assert {i["artifact"] for i in doc["imports"]} == {"reports/eval/cohort_scan.json"} | {
        f"reports/eval/{n}" for n in score.SCORECARDS}  # fmt: skip
    assert all(i["produced_by"] == MADRE and i["accreditation"] == "importado" for i in doc["imports"])


def test_fail_es_terminal_y_un_reintento_es_otra_complementaria(mundo: dict, capsys) -> None:
    _en_curso(mundo, "e3", capsys)
    assert _cli(mundo, "fail", "--reason", "lane muerto", capsys=capsys)[1]["status"] == "failed"
    rc, out = _cli(mundo, "start", "--parent-root", str(mundo["madre"]), capsys=capsys)
    assert rc == 2 and out["codes"] == ["transicion"]


# ═══════════════════════════════ C3 · conformal consume importaciones explícitas
def _conformal_dobles(monkeypatch, reports: Path) -> None:
    """`_evaluate` y la carga de series son productores: dobles deterministas."""
    pooled = {"cov": 0.92, "ci": (0.9, 0.94), "n": 120, "insufficient_n": False}
    r = {"gamma": 0.005, "point_mase": 1.1, "split_cov": 0.93, "split_width": 2.0, "split_pooled": pooled,
         "aci_cov": 0.92, "aci_width": 2.1, "aci_pooled": pooled, "n": len(PAISES)}  # fmt: skip
    monkeypatch.setattr(ic, "REPORTS", reports)
    monkeypatch.chdir(reports.parent)  # `dataset.DB_PATH` es relativa: el operador corre desde la raíz del árbol
    monkeypatch.setattr(ic, "_evaluate", lambda table: dict(r))
    serie = pd.Series([1.0, 3.0, 1.5], index=pd.to_datetime(["2025-01-01", "2025-02-01", "2025-03-01"]))
    monkeypatch.setattr(ic.dataset, "load_series", lambda *a, **k: serie)


@pytest.fixture
def madre_con_conformal(mundo: dict, monkeypatch) -> dict:
    """La madre corrió conformal SIN deep-PI acreditado (baseline vacío): lo que C3 viene a completar."""
    with monkeypatch.context() as mp:
        _conformal_dobles(mp, mundo["madre"] / "reports")
        ic.main([])
    mundo["huellas_madre"] = _huellas(mundo["madre"])
    return mundo


def test_c3_ciclo_completo_baseline_importado_y_rotulado(madre_con_conformal: dict, monkeypatch, capsys) -> None:
    m = madre_con_conformal
    _en_curso(m, "deep_pi_conformal", capsys, cid=ID_C3)
    _conformal_dobles(monkeypatch, m["compl"] / "reports")
    ic.main(["--complement", "--parent-root", str(m["madre"])])
    filas = pd.read_csv(m["compl"] / cx.CONFORMAL_REL).set_index("table")
    assert filas.loc["FAD", "baseline_coverage"] == round(2 / 3, 4), "cubre 1 y 1.5; 3 queda fuera de [0, 2]"
    sidecar = json.loads((m["compl"] / cx.PROCEDENCIA_REL).read_text())
    assert {c["source"] for t in sidecar["tables"].values() for c in t.values()} == {f"importado:{MADRE}"}
    assert _cli(m, "close", "--parent-root", str(m["madre"]), capsys=capsys)[1]["status"] == "computed"
    assert _cli(m, "accept", "--parent-root", str(m["madre"]), capsys=capsys)[1]["status"] == "accredited"
    assert _huellas(m["madre"]) == m["huellas_madre"]


def test_c3_con_un_deep_pi_sustituido_no_escribe_nada(madre_con_conformal: dict, monkeypatch, capsys) -> None:
    m = madre_con_conformal
    _en_curso(m, "deep_pi_conformal", capsys, cid=ID_C3)
    _escribir(m["compl"], cx.artefacto_c3("baseline", "DFF")[0], _deep_pi("DFF").replace("2.0\n", "9.0\n"))
    _conformal_dobles(monkeypatch, m["compl"] / "reports")
    with pytest.raises(SystemExit, match="importacion-sustituida"):
        ic.main(["--complement", "--parent-root", str(m["madre"])])
    assert not (m["compl"] / cx.CONFORMAL_REL).exists() and not (m["compl"] / cx.PROCEDENCIA_REL).exists()


def test_conformal_sin_bandera_se_niega_en_un_arbol_con_complementaria(
    madre_con_conformal, monkeypatch, capsys
) -> None:
    m = madre_con_conformal
    _en_curso(m, "deep_pi_conformal", capsys, cid=ID_C3)
    _conformal_dobles(monkeypatch, m["compl"] / "reports")
    with pytest.raises(SystemExit, match="--complement"):
        ic.main([])
    assert not (m["compl"] / cx.CONFORMAL_REL).exists()


@pytest.mark.parametrize("caso", ["planificada", "de_e3"])
def test_conformal_solo_corre_en_una_c3_en_curso(madre_con_conformal: dict, monkeypatch, capsys, caso: str) -> None:
    m = madre_con_conformal
    if caso == "planificada":
        _planificada(m, "deep_pi_conformal", capsys, cid=ID_C3)
    else:
        _en_curso(m, "e3", capsys)
    _conformal_dobles(monkeypatch, m["compl"] / "reports")
    with pytest.raises(SystemExit, match="no-en-curso" if caso == "planificada" else "proposito"):
        ic.main(["--complement", "--parent-root", str(m["madre"])])
    assert not (m["compl"] / cx.CONFORMAL_REL).exists()


def test_c3_cuyo_gamma_cambia_no_cierra(madre_con_conformal: dict, monkeypatch, capsys) -> None:
    """Si conformal no reproduce gamma sobre las mismas entradas, es un hallazgo: C3 queda en failed."""
    m = madre_con_conformal
    _en_curso(m, "deep_pi_conformal", capsys, cid=ID_C3)
    _conformal_dobles(monkeypatch, m["compl"] / "reports")
    evaluar = ic._evaluate
    monkeypatch.setattr(ic, "_evaluate", lambda table: {**evaluar(table), "gamma": 0.01})
    ic.main(["--complement", "--parent-root", str(m["madre"])])
    rc, out = _cli(m, "close", "--parent-root", str(m["madre"]), capsys=capsys)
    assert rc == 2 and {"gamma", "conformal-fuera-de-alcance"} <= set(out["codes"])
    assert cx.leer(m["compl"])["status"] == "failed"


# ═══════════════════════════════ código permitido en TODO el ciclo (adenda Codex 9-oct)
_EDICIONES = {
    "modificado": lambda r: _escribir(r, "experiments/codigo.py", "X = 2\n"),
    "nuevo_sin_versionar": lambda r: _escribir(r, "vp_model/parche.py", "Y = 1\n"),
    "borrado": lambda r: (r / "experiments/codigo.py").unlink(),
}


def _c4_hasta(m: dict, monkeypatch, capsys, etapa: str) -> None:
    """C4 llevado con la CLI y el orquestador reales hasta ``etapa`` (running con lanes, computed o accredited)."""
    _en_curso(m, "e3", capsys)
    _lanes_dobles(m, monkeypatch)
    assert e3.main(["--complement"]) == 0
    for paso, estado in (("close", "computed"), ("accept", "accredited")):
        if etapa == "running" or (etapa == "computed" and paso == "accept"):
            break
        assert _cli(m, paso, "--parent-root", str(m["madre"]), capsys=capsys)[1]["status"] == estado


@pytest.mark.parametrize("edicion", sorted(_EDICIONES))
def test_RED_codigo_editado_tras_start_no_cierra(mundo: dict, monkeypatch, capsys, edicion: str) -> None:
    """El RED de la adenda: HEAD igual, árbol sucio. Antes, `close`/`accept`/`verify` daban rc 0."""
    _c4_hasta(mundo, monkeypatch, capsys, "running")
    _EDICIONES[edicion](mundo["compl"])
    assert cx.estado_git(mundo["compl"])[1] is True
    rc, out = _cli(mundo, "close", "--parent-root", str(mundo["madre"]), capsys=capsys)
    assert rc == 2 and "codigo-sucio" in out["codes"]
    assert cx.leer(mundo["compl"])["status"] == "failed"


def test_RED_codigo_editado_tras_computed_no_se_acepta(mundo: dict, monkeypatch, capsys) -> None:
    _c4_hasta(mundo, monkeypatch, capsys, "computed")
    _EDICIONES["modificado"](mundo["compl"])
    rc, out = _cli(mundo, "accept", "--parent-root", str(mundo["madre"]), capsys=capsys)
    assert rc == 2 and "codigo-sucio" in out["codes"]
    assert cx.leer(mundo["compl"])["status"] == "failed"


def test_RED_codigo_editado_tras_accredited_no_se_verifica_ni_se_puntua(mundo: dict, monkeypatch, capsys) -> None:
    _c4_hasta(mundo, monkeypatch, capsys, "accredited")
    _EDICIONES["nuevo_sin_versionar"](mundo["compl"])
    rc, out = _cli(mundo, "verify", "--parent-root", str(mundo["madre"]), capsys=capsys)
    assert rc == 2 and out["codes"] == ["codigo-sucio"]
    with pytest.raises(score.E3NoAcreditado, match="codigo-sucio"):
        score.puntuar(root=mundo["compl"], complemento=mundo["madre"])
    assert cx.leer(mundo["compl"])["status"] == "accredited", "el lector rechaza sin escribir"
    (mundo["compl"] / "vp_model/parche.py").unlink()
    assert _cli(mundo, "verify", "--parent-root", str(mundo["madre"]), capsys=capsys)[0] == 0, (
        "el lector mide el árbol cada vez: no es un cerrojo"
    )


# ═══════════════════════════════ productores: antes de calcular
def test_RED_orquestador_con_codigo_sucio_no_lanza_lanes(mundo: dict, monkeypatch, capsys) -> None:
    _en_curso(mundo, "e3", capsys)
    _EDICIONES["modificado"](mundo["compl"])
    llamadas = _lanes_dobles(mundo, monkeypatch)
    with pytest.raises(SystemExit, match="codigo-sucio"):
        e3.main(["--complement"])
    assert llamadas == []


def test_RED_orquestador_con_head_movido_no_lanza_lanes(mundo: dict, monkeypatch, capsys) -> None:
    _en_curso(mundo, "e3", capsys)
    _EDICIONES["modificado"](mundo["compl"])
    _git(mundo["compl"], "commit", "-qam", "después de start")
    llamadas = _lanes_dobles(mundo, monkeypatch)
    with pytest.raises(SystemExit, match=r"\[codigo\]"):
        e3.main(["--complement"])
    assert llamadas == []


def test_RED_orquestador_se_detiene_si_el_codigo_cambia_entre_lanes(mundo: dict, monkeypatch, capsys) -> None:
    _en_curso(mundo, "e3", capsys)
    llamadas = _lanes_dobles(mundo, monkeypatch, despues=_EDICIONES["modificado"])
    with pytest.raises(SystemExit, match="codigo-sucio"):
        e3.main(["--complement"])
    assert len(llamadas) == 1, "el segundo lane no se lanza"


def test_RED_orquestador_con_duckdb_cambiada_no_lanza_lanes(mundo: dict, tmp_path: Path, monkeypatch, capsys) -> None:
    _en_curso(mundo, "e3", capsys)
    db = mundo["compl"] / cx.DUCKDB_REL
    db.unlink()
    mm.construir_duckdb(db, mundo["compl"] / cx.PANEL_REL, tmp_path / "build2", meses=29)
    assert cx.estado_git(mundo["compl"])[1] is False, "la DuckDB es ignorada: git no la ve, el sello sí"
    llamadas = _lanes_dobles(mundo, monkeypatch)
    with pytest.raises(SystemExit, match="duckdb-cambio"):
        e3.main(["--complement"])
    assert llamadas == []
    rc, out = _cli(mundo, "close", "--parent-root", str(mundo["madre"]), capsys=capsys)
    assert rc == 2 and "duckdb-cambio" in out["codes"]


# ── vistas: mismas filas, otra semántica (adenda Codex 9-oct); git no las ve, el sello sí
@pytest.mark.parametrize("vista", sorted(mm.CAMBIOS_DE_VISTA))
def test_RED_orquestador_con_vista_redefinida_no_lanza_lanes(mundo: dict, monkeypatch, capsys, vista: str) -> None:
    _en_curso(mundo, "e3", capsys)
    mm.redefinir_vista(mundo["compl"] / cx.DUCKDB_REL, vista)
    assert cx.estado_git(mundo["compl"])[1] is False
    llamadas = _lanes_dobles(mundo, monkeypatch)
    with pytest.raises(SystemExit, match="duckdb-catalogo"):
        e3.main(["--complement"])
    assert llamadas == []
    rc, out = _cli(mundo, "close", "--parent-root", str(mundo["madre"]), capsys=capsys)
    assert rc == 2 and "duckdb-catalogo" in out["codes"]
    assert cx.leer(mundo["compl"])["status"] == "failed"


def test_RED_vista_redefinida_entre_lanes_detiene_el_orquestador(mundo: dict, monkeypatch, capsys) -> None:
    _en_curso(mundo, "e3", capsys)
    db = mundo["compl"] / cx.DUCKDB_REL
    llamadas = _lanes_dobles(mundo, monkeypatch, despues=lambda _r: mm.redefinir_vista(db, "mart_training_F"))
    with pytest.raises(SystemExit, match="duckdb-catalogo"):
        e3.main(["--complement"])
    assert len(llamadas) == 1, "el segundo lane no se lanza"


def test_RED_vista_redefinida_tras_computed_no_se_acepta(mundo: dict, monkeypatch, capsys) -> None:
    _c4_hasta(mundo, monkeypatch, capsys, "computed")
    mm.redefinir_vista(mundo["compl"] / cx.DUCKDB_REL, "mart_series_summary")
    rc, out = _cli(mundo, "accept", "--parent-root", str(mundo["madre"]), capsys=capsys)
    assert rc == 2 and out["codes"] == ["duckdb-catalogo"]
    assert cx.leer(mundo["compl"])["status"] == "failed"


def test_RED_vista_redefinida_tras_accredited_no_se_verifica_ni_se_puntua(mundo: dict, monkeypatch, capsys) -> None:
    _c4_hasta(mundo, monkeypatch, capsys, "accredited")
    mm.redefinir_vista(mundo["compl"] / cx.DUCKDB_REL, "mart_training_F")
    rc, out = _cli(mundo, "verify", "--parent-root", str(mundo["madre"]), capsys=capsys)
    assert rc == 2 and out["codes"] == ["duckdb-catalogo"]
    with pytest.raises(score.E3NoAcreditado, match="duckdb-catalogo"):
        score.puntuar(root=mundo["compl"], complemento=mundo["madre"])
    assert cx.leer(mundo["compl"])["status"] == "accredited", "el lector rechaza sin escribir"


@pytest.mark.parametrize("vista", sorted(mm.CAMBIOS_DE_VISTA))
def test_RED_conformal_con_vista_redefinida_no_calcula(
    madre_con_conformal: dict, monkeypatch, capsys, vista: str
) -> None:
    m = madre_con_conformal
    _en_curso(m, "deep_pi_conformal", capsys, cid=ID_C3)
    mm.redefinir_vista(m["compl"] / cx.DUCKDB_REL, vista)
    _conformal_dobles(monkeypatch, m["compl"] / "reports")
    with pytest.raises(SystemExit, match="duckdb-catalogo"):
        ic.main(["--complement", "--parent-root", str(m["madre"])])
    assert not (m["compl"] / cx.CONFORMAL_REL).exists() and not (m["compl"] / cx.PROCEDENCIA_REL).exists()


def test_control_los_lanes_heredan_el_pythonpath_del_arbol(mundo: dict, monkeypatch, capsys) -> None:
    """Sin él, el GBM importaría `vp_model` de la instalación editable del venv: otro checkout."""
    _en_curso(mundo, "e3", capsys)
    monkeypatch.setenv("PYTHONPATH", "/otro/checkout")
    entornos: list[dict] = []
    _lanes_dobles(mundo, monkeypatch, entornos=entornos)
    assert e3.main(["--complement"]) == 0
    assert {e["PYTHONPATH"] for e in entornos} == {os.pathsep.join((str(mundo["compl"]), "/otro/checkout"))}


def test_RED_conformal_con_codigo_sucio_no_calcula(madre_con_conformal: dict, monkeypatch, capsys) -> None:
    m = madre_con_conformal
    _en_curso(m, "deep_pi_conformal", capsys, cid=ID_C3)
    _conformal_dobles(monkeypatch, m["compl"] / "reports")
    _EDICIONES["borrado"](m["compl"])
    with pytest.raises(SystemExit, match="codigo-sucio"):
        ic.main(["--complement", "--parent-root", str(m["madre"])])
    assert not (m["compl"] / cx.CONFORMAL_REL).exists() and not (m["compl"] / cx.PROCEDENCIA_REL).exists()


def test_RED_conformal_desde_otro_cwd_abriria_otra_duckdb(
    madre_con_conformal: dict, tmp_path, monkeypatch, capsys
) -> None:
    m = madre_con_conformal
    _en_curso(m, "deep_pi_conformal", capsys, cid=ID_C3)
    _conformal_dobles(monkeypatch, m["compl"] / "reports")
    monkeypatch.chdir(m["madre"])  # su DuckDB relativa sería la de otro árbol
    with pytest.raises(SystemExit, match="duckdb-ajena"):
        ic.main(["--complement", "--parent-root", str(m["madre"])])
    assert not (m["compl"] / cx.CONFORMAL_REL).exists()


def test_RED_conformal_con_duckdb_cambiada_no_calcula(madre_con_conformal: dict, tmp_path, monkeypatch, capsys) -> None:
    m = madre_con_conformal
    _en_curso(m, "deep_pi_conformal", capsys, cid=ID_C3)
    db = m["compl"] / cx.DUCKDB_REL
    db.unlink()
    mm.construir_duckdb(db, m["compl"] / cx.PANEL_REL, tmp_path / "build2", git_sha="b" * 40)
    _conformal_dobles(monkeypatch, m["compl"] / "reports")
    with pytest.raises(SystemExit, match="duckdb-cambio"):
        ic.main(["--complement", "--parent-root", str(m["madre"])])
    assert not (m["compl"] / cx.CONFORMAL_REL).exists()


def test_RED_codigo_editado_durante_conformal_no_firma_ni_cierra(
    madre_con_conformal: dict, monkeypatch, capsys
) -> None:
    m = madre_con_conformal
    _en_curso(m, "deep_pi_conformal", capsys, cid=ID_C3)
    _conformal_dobles(monkeypatch, m["compl"] / "reports")
    evaluar = ic._evaluate

    def evaluar_y_editar(table: str) -> dict:
        _EDICIONES["modificado"](m["compl"])
        return evaluar(table)

    monkeypatch.setattr(ic, "_evaluate", evaluar_y_editar)
    with pytest.raises(SystemExit, match=r"(?s)no firma su procedencia.*codigo-sucio"):
        ic.main(["--complement", "--parent-root", str(m["madre"])])
    assert not (m["compl"] / cx.PROCEDENCIA_REL).exists()
    rc, out = _cli(m, "close", "--parent-root", str(m["madre"]), capsys=capsys)
    assert rc == 2 and {"codigo-sucio", "conformal-procedencia"} <= set(out["codes"])


# ═══════════════════════════════ identidad que firma cada lane
def test_RED_un_lane_no_firma_con_codigo_sucio(mundo: dict, capsys) -> None:
    _en_curso(mundo, "e3", capsys)
    _EDICIONES["nuevo_sin_versionar"](mundo["compl"])
    with pytest.raises(hwr.WinnerReceiptError, match="codigo-sucio"):
        hwr.lane_campaign_identity(mundo["compl"])


def test_RED_un_lane_no_firma_con_codigo_cargado_de_otro_checkout(mundo: dict, monkeypatch, capsys) -> None:
    """Sin parche: este proceso cargó `vp_model` de RAIZ, no del worktree complementario."""
    _en_curso(mundo, "e3", capsys)
    monkeypatch.setattr(cx, "codigo_cargado", CODIGO_CARGADO)
    with pytest.raises(hwr.WinnerReceiptError, match="codigo-ajeno"):
        hwr.lane_campaign_identity(mundo["compl"])
    llamadas = _lanes_dobles(mundo, monkeypatch)
    with pytest.raises(SystemExit, match="codigo-ajeno"):
        e3.main(["--complement"])
    assert llamadas == []


def test_RED_recibo_firmado_con_otro_commit_no_cierra(mundo: dict, monkeypatch, capsys) -> None:
    _en_curso(mundo, "e3", capsys)
    ajena = {**cx.identidad(cx.leer(mundo["compl"])), "source_git_sha": "f" * 40}
    _lanes_dobles(mundo, monkeypatch, identidad=ajena)
    e3.main(["--complement"])
    rc, out = _cli(mundo, "close", "--parent-root", str(mundo["madre"]), capsys=capsys)
    assert rc == 2 and out["codes"] == ["lanes"]


# ═══════════════════════════════ controles benignos: salidas legítimas
def test_control_salidas_legitimas_e_ignoradas_no_ensucian(mundo: dict, monkeypatch, capsys) -> None:
    """Lo que la auditoría de escrituras encontró: bajo reports/ y en rutas que el `.gitignore` real excluye."""
    _en_curso(mundo, "e3", capsys)
    _lanes_dobles(mundo, monkeypatch)
    assert e3.main(["--complement"]) == 0
    for rel in ("experiments/__pycache__/codigo.cpython-314.pyc", "mlruns_staging/improve_FAD.jsonl",
                "lightning_logs/version_0/hparams.yaml", "reports/campaign/e3/e3_FAD_all_diff.csv",
                "reports/campaign/hpo_deep_trials_FAD_x.csv", "reports/eval/e3_gbm_all_FAD.csv"):  # fmt: skip
        _escribir(mundo["compl"], rel, "x\n")
    assert cx.estado_git(mundo["compl"])[1] is False
    assert _cli(mundo, "close", "--parent-root", str(mundo["madre"]), capsys=capsys)[1]["status"] == "computed"
    assert _cli(mundo, "accept", "--parent-root", str(mundo["madre"]), capsys=capsys)[1]["status"] == "accredited"


def test_RED_un_gitignore_editado_para_esconder_codigo_es_codigo_sucio(mundo: dict, capsys) -> None:
    """La regla que decide qué se ignora es código: ampliarla para tapar un archivo ensucia el árbol."""
    _en_curso(mundo, "e3", capsys)
    gi = mundo["compl"] / ".gitignore"
    gi.write_text(gi.read_text() + "vp_model/parche.py\n")
    _EDICIONES["nuevo_sin_versionar"](mundo["compl"])
    assert cx.estado_git(mundo["compl"])[1] is True


# ═══════════════════════════════ evidencia por la CLI
def test_RED_plan_con_un_cuaderno_ausente_del_tar_de_la_madre(mundo: dict, capsys) -> None:
    assert _cli(mundo, "import", "--parent-root", str(mundo["madre"]), "--purpose", "e3", capsys=capsys)[0] == 0
    _evidencia(mundo, "e3", sin=f"reports/eval/{score.SCORECARDS[0]}")
    rc, out = _cli(mundo, "plan", "--parent-root", str(mundo["madre"]), "--id", ID_E3, "--purpose", "e3",
                   "--evidence", EVID, capsys=capsys)  # fmt: skip
    assert rc == 2 and out["codes"] == ["importacion-tar-ausente"]
    assert not (mundo["compl"] / cx.RUTA_REL).exists()
