"""M74-F · deudas 2 y 3 — E3 se acredita al leer y corre con la semilla de la baraja.

Medido sobre la cuarta campaña real (`rederiv_953cd00_20261006T073927`): 42/42 recibos, **ninguno con
identidad de campaña**, y los 6 lanes GBM con ``seed=42`` cuando la baraja pre-registra ``[1]``.
`score_e3_campaign` los puntuó por existencia. Estas pruebas fijan:

* el plan (`Deck.lanes()`) lleva la semilla de la baraja y cada orden real la pasa explícita;
* con receta, ningún runner corre con otra semilla;
* el recibo lleva la identidad de la transacción `running` (o ``None`` fuera de campaña, nunca inventada);
* el puntuador exige el conjunto EXACTO del plan, la semilla, la identidad y los hashes de salida.

Todo en directorios temporales: ninguna prueba toca `reports/` vivo ni entrena.
"""

from __future__ import annotations

import hashlib
import json
import sys
from importlib.util import find_spec
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "experiments"))

from vp_model.deck import cargar_deck  # noqa: E402

IDENT = {"campaign_id": "rederiv_abc_1", "source_git_sha": "b" * 40, "panel_sha256": "sha256:" + "c" * 64}


# ═══════════════════════════════ deuda 3 · la semilla
def test_la_baraja_declara_una_semilla_y_el_plan_la_lleva() -> None:
    deck = cargar_deck()
    assert deck.semilla() == 1
    lanes = deck.lanes()
    assert len(lanes) == 42 and {s.seed for s in lanes} == {1}


def test_varias_semillas_rompen_el_plan_antes_de_entrenar() -> None:
    from dataclasses import replace

    with pytest.raises(ValueError, match="admite una"):
        replace(cargar_deck(), seeds=(1, 2)).lanes()


def test_cada_orden_real_pasa_la_semilla_de_la_baraja() -> None:
    import run_e3_campaign as e3

    for lane in e3.plan():
        cmd = lane.command
        assert cmd[cmd.index("--seed") + 1] == "1", lane


def test_el_plan_del_orquestador_es_el_de_la_baraja() -> None:
    import run_e3_campaign as e3

    assert [(x.recipe, x.role, x.cohort, x.table, x.runner, x.seed) for x in e3.plan()] == [
        (s.recipe, s.role, s.cohort, s.table, s.runner, s.seed) for s in cargar_deck().lanes()
    ]


@pytest.mark.skipif(find_spec("lightgbm") is None or find_spec("darts") is None, reason="runner GBM del extra model")
def test_RED_el_lane_gbm_ya_no_corre_con_su_semilla_por_defecto() -> None:
    """Contra `953cd00` esto pasaba: `_receta_gbm` aceptaba la semilla 42 del parser."""
    import run_global_gbm as gbm

    args = gbm.build_parser().parse_args(["--recipe", "control-gbm-lightgbm", "--table", "FAD"])
    assert args.seed == 42, "el defecto del runner sigue siendo 42 para corridas sin receta"
    with pytest.raises(SystemExit, match="semilla 42"):
        gbm._receta_gbm(args)
    # control benigno: con la semilla de la baraja, la receta se aplica
    ok = gbm.build_parser().parse_args(["--recipe", "control-gbm-lightgbm", "--table", "FAD", "--seed", "1"])
    gbm._receta_gbm(ok)
    assert ok.models == ["lightgbm"]


@pytest.mark.skipif(find_spec("lightgbm") is None or find_spec("darts") is None, reason="runner GBM del extra model")
def test_cada_orden_gbm_real_parsea_y_aplica_su_receta() -> None:
    import run_e3_campaign as e3
    import run_global_gbm as gbm

    gbm_lanes = [x for x in e3.plan() if x.runner == "run_global_gbm.py"]
    assert len(gbm_lanes) == 6
    for lane in gbm_lanes:
        args = gbm.build_parser().parse_args(lane.command[2:])
        gbm._receta_gbm(args)  # no SystemExit: la orden real cumple la baraja


def test_aplicar_receta_deep_exige_la_semilla_de_la_baraja() -> None:
    pytest.importorskip("pandas")
    import argparse

    import run_global_deep as deep

    deck = cargar_deck()
    receta = deck.receta("control-bitcn")
    with pytest.raises(SystemExit, match="semilla 42"):
        deep.aplicar_receta(argparse.Namespace(seed=42), receta, deck)
    args = deep.aplicar_receta(deep.build_parser().parse_args(["--recipe", "control-bitcn"]), receta, deck)
    assert args.seed == 1 and args.universe == deck.universe


# ═══════════════════════════════ deuda 2 · la identidad que escribe el lane
def _transaccion(root: Path, status: str = "running", cid: str = IDENT["campaign_id"]) -> None:
    (root / "reports" / "campaign").mkdir(parents=True, exist_ok=True)
    (root / "reports" / "campaign" / "campaign.json").write_text(
        json.dumps({**IDENT, "campaign_id": cid, "status": status})
    )


def test_fuera_de_campana_el_lane_no_inventa_identidad(tmp_path: Path) -> None:
    from hpo_winner_receipt import lane_campaign_identity

    assert lane_campaign_identity(tmp_path) is None
    _transaccion(tmp_path, status="computed")
    assert lane_campaign_identity(tmp_path) is None, "una campaña terminada no presta su identidad"


def test_dentro_de_campana_el_lane_escribe_la_identidad_de_la_transaccion(tmp_path: Path, monkeypatch) -> None:
    from hpo_winner_receipt import WinnerReceiptError, lane_campaign_identity

    _transaccion(tmp_path)
    monkeypatch.setenv("CAMPAIGN_ID", IDENT["campaign_id"])
    assert lane_campaign_identity(tmp_path) == IDENT
    monkeypatch.setenv("CAMPAIGN_ID", "rederiv_otra")
    with pytest.raises(WinnerReceiptError, match="otra corrida"):
        lane_campaign_identity(tmp_path)


# ═══════════════════════════════ deuda 2 · el lector acredita
def _score():
    pytest.importorskip("darts")
    import score_e3_campaign

    return score_e3_campaign


def _deck_en(root: Path):
    """La baraja real con sus entradas declaradas re-ancladas a archivos del árbol temporal."""
    from dataclasses import replace

    from vp_model.deck import RUTAS_ENTRADAS

    deck = cargar_deck()
    entradas = {}
    for clave in deck.inputs:
        if clave in {"commit", "deck_version"}:
            entradas[clave] = deck.inputs[clave]
            continue
        ruta = root / RUTAS_ENTRADAS.get(clave, clave)
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(f"contenido de {clave}\n")
        entradas[clave] = hashlib.sha256(ruta.read_bytes()).hexdigest()
    return replace(deck, inputs=entradas)


def _recibos(root: Path, *, mutar=None) -> Path:
    """Los 42 recibos del plan, con identidad y una salida real cada uno — la forma que dejan los runners."""
    directorio = root / "reports" / "campaign" / "e3"
    directorio.mkdir(parents=True)
    for spec in cargar_deck().lanes():
        salida = root / "reports" / "eval" / f"out_{spec.recipe}_{spec.table}_{spec.cohort}.csv"
        salida.parent.mkdir(parents=True, exist_ok=True)
        salida.write_text(f"{spec.recipe},{spec.table},{spec.cohort}\n")
        recibo = {
            "recipe": spec.recipe,
            "recipe_role": spec.role,
            "cohort": spec.cohort,
            "table": spec.table,
            "seed": spec.seed,
            "campaign": dict(IDENT),
            "status": "ok",
            "outputs": [
                {"path": str(salida.relative_to(root)), "sha256": hashlib.sha256(salida.read_bytes()).hexdigest()}
            ],
        }
        if mutar:
            mutar(spec, recibo)
        (directorio / spec.receipt_name).write_text(json.dumps(recibo))
    return directorio


def test_control_benigno_el_plan_completo_y_acreditado_pasa(tmp_path: Path) -> None:
    score = _score()
    d = _recibos(tmp_path)
    assert len(score.acreditar_recibos(d, _deck_en(tmp_path), IDENT, root=tmp_path)) == 42


def test_RED_recibos_sin_identidad_como_los_de_la_cuarta_campana_no_se_puntuan(tmp_path: Path) -> None:
    score = _score()
    d = _recibos(tmp_path, mutar=lambda _s, r: r.pop("campaign"))
    with pytest.raises(score.E3NoAcreditado, match="42 problema"):
        score.acreditar_recibos(d, _deck_en(tmp_path), IDENT, root=tmp_path)


def test_RED_la_semilla_42_de_los_gbm_no_pasa(tmp_path: Path) -> None:
    score = _score()

    def gbm42(spec, r):
        if spec.runner == "run_global_gbm.py":
            r["seed"] = 42

    d = _recibos(tmp_path, mutar=gbm42)
    with pytest.raises(score.E3NoAcreditado, match="6 problema") as exc:
        score.acreditar_recibos(d, _deck_en(tmp_path), IDENT, root=tmp_path)
    assert "seed=42" in str(exc.value)


def test_un_recibo_de_otra_campana_no_pasa(tmp_path: Path) -> None:
    score = _score()
    d = _recibos(tmp_path)
    with pytest.raises(score.E3NoAcreditado, match="42 problema"):
        score.acreditar_recibos(d, _deck_en(tmp_path), {**IDENT, "campaign_id": "rederiv_otra"}, root=tmp_path)


def test_ausentes_y_sobrantes_no_pasan(tmp_path: Path) -> None:
    score = _score()
    d = _recibos(tmp_path)
    next(d.glob("receipt_control-tide_*.json")).unlink()
    (d / "receipt_receta-fantasma_FAD_all.json").write_text("{}")
    with pytest.raises(score.E3NoAcreditado) as exc:
        score.acreditar_recibos(d, _deck_en(tmp_path), IDENT, root=tmp_path)
    assert "falta el recibo" in str(exc.value) and "fuera del plan" in str(exc.value)


def test_una_salida_tocada_despues_del_lane_no_pasa(tmp_path: Path) -> None:
    score = _score()
    d = _recibos(tmp_path)
    next((tmp_path / "reports" / "eval").glob("out_control-bitcn_FAD_all.csv")).write_text("otra cosa\n")
    with pytest.raises(score.E3NoAcreditado, match="cambió desde"):
        score.acreditar_recibos(d, _deck_en(tmp_path), IDENT, root=tmp_path)


def test_un_recibo_que_cambia_de_receta_no_pasa(tmp_path: Path) -> None:
    score = _score()

    def cambia(spec, r):
        if spec.receipt_name == "receipt_control-tide_FAD_all.json":
            r["recipe"] = "control-bitcn"

    d = _recibos(tmp_path, mutar=cambia)
    with pytest.raises(score.E3NoAcreditado, match="1 problema"):
        score.acreditar_recibos(d, _deck_en(tmp_path), IDENT, root=tmp_path)


def test_el_puntuador_sin_campana_en_curso_falla_cerrado(tmp_path: Path) -> None:
    score = _score()
    with pytest.raises(score.E3NoAcreditado, match="sin campaña en curso"):
        score.campana_activa(tmp_path)


# ═══════════════════════════════ deuda 7 · entradas congeladas frente a derivadas en campaña
def test_una_entrada_congelada_que_cambia_bloquea_e3(tmp_path: Path) -> None:
    score = _score()
    deck = _deck_en(tmp_path)
    d = _recibos(tmp_path)
    (tmp_path / "data" / "processed" / "visa_panel_long.csv").write_text("otro panel\n")
    with pytest.raises(score.E3NoAcreditado, match="entrada congelada visa_panel_long.csv: difiere"):
        score.acreditar_recibos(d, deck, IDENT, root=tmp_path)


def test_RED_cohort_scan_rederivado_en_campana_se_delimita_sin_bloquear(tmp_path: Path) -> None:
    """La cuarta campaña: `cohort_scan.json` pasó de 806b3fed… a 9dc3cf12… porque 8c lo re-deriva."""
    score = _score()
    deck = _deck_en(tmp_path)
    d = _recibos(tmp_path)
    (tmp_path / "reports" / "eval" / "cohort_scan.json").write_text("re-derivado por la campaña\n")
    assert len(score.acreditar_recibos(d, deck, IDENT, root=tmp_path)) == 42
    c = deck.contrastar_entradas(tmp_path)["cohort_scan.json"]
    assert c["kind"] == "derivada_en_campana" and c["status"] == "difiere"
    assert c["declared"] != c["live"], "los dos hashes quedan registrados: eso es delimitar"


def test_en_el_arbol_vivo_solo_cohort_scan_y_series_cohorts_son_derivadas() -> None:
    contraste = cargar_deck().contrastar_entradas(RAIZ)
    derivadas = {k for k, c in contraste.items() if c["kind"] == "derivada_en_campana"}
    assert derivadas == {"cohort_scan.json", "series_cohorts.json"}
