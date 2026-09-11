"""M74-A · El preflight de la campaña causal: sella entradas, reconcilia protocolo y ensaya el mecanismo.

Nada aquí ejecuta la campaña real ni escribe en `reports/`. Se comprueban tres cosas:

1. **Las entradas se derivan, no se listan.** El cierre de código sale del runbook por AST; los
   datos, del DAG; y la gobernanza está declarada por nombre **con su ancla al módulo que la
   nombra**, de modo que mover una constante rompa el sello en vez de sellar un archivo muerto.
2. **El protocolo se lee de sus autoridades vivas**, y los campos volátiles quedan fuera para que
   dos preflights seguidos coincidan.
3. **El mecanismo se ensaya en un temporal**: el bloque de transacción REAL del runbook, con un
   desenlace sintético, recorriendo `running → computed → validated` y comprobando que la
   publicación sólo se abre al final.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path

import pytest
import yaml

from tools import campaign_preflight as pf
from tools import campaign_state as cs

ROOT = Path(__file__).resolve().parent.parent
#: `vp_model.champion` importa `scipy` al cargarse, y el job base instala sólo `.[dev]`.
#: Sólo las dos pruebas que lo tocan se saltan ahí; el resto del archivo corre en los dos jobs
#: (medido con el extra bloqueado: 12 corren, 2 saltan).
NECESITA_EXTRA = pytest.mark.skipif(find_spec("scipy") is None, reason="requiere el extra `model` (scipy)")


# --------------------------------------------------------------- entradas derivadas
def test_entrypoints_come_from_the_runbook_and_follow_its_nested_shells() -> None:
    """El descubrimiento sigue los shells anidados; antes leía sólo el guion de arriba (H1)."""
    eps = pf.runbook_entrypoints()
    assert "experiments/run_champion_challenger.py" in eps["scripts"]  # directo
    assert "vp_model.confirm_tuning" in eps["modules"]
    assert "experiments/run_campaign.sh" in eps["shell"]
    # ★ alcanzados SÓLO por recursión: los invoca `run_campaign.sh` / `save_finalists.sh`
    for anidado in (
        "experiments/run_global_deep.py",
        "experiments/aggregate_seeds.py",
        "experiments/save_finalists_deep.py",
        "experiments/export_forecasts.py",
    ):
        assert anidado in eps["scripts"], f"{anidado} sólo se alcanza siguiendo los shells"
    assert "vp_model.run_comparison" in eps["modules"]
    # Los comentarios del runbook nombran scripts en su cabecera; la lectura los ignora. Se
    # comprueba con un guion sembrado, no sobre el archivo real, para que discrimine de verdad.
    sembrado = ROOT / "experiments" / "run_rederivation.sh"
    assert any(
        "experiments/" in ln for ln in sembrado.read_text(encoding="utf-8").splitlines() if ln.lstrip().startswith("#")
    )


def test_code_closure_is_transitive() -> None:
    cierre = set(pf.code_closure(pf.runbook_entrypoints()))
    assert "experiments/run_champion_challenger.py" in cierre  # entrypoint directo
    # alcanzados SÓLO por transitividad: ninguno lo invoca el runbook
    for indirecto in ("vp_model/config.py", "vp_model/metrics.py", "vp_model/scale.py", "vp_model/walkforward.py"):
        assert indirecto in cierre, f"{indirecto} debería entrar por cierre transitivo"
    # los guiones de shell y el propio runbook también se sellan
    assert "experiments/run_campaign.sh" in cierre and "experiments/run_rederivation.sh" in cierre


def test_which_of_the_cohort_chain_the_campaign_reaches_and_which_it_does_not() -> None:
    """★ Corregido en M74-B: mi afirmación de M74-A era **falsa en un módulo**.

    Dije «el runbook no alcanza la cadena E». Cierto para `stability`, `universe`, `horizon` y
    `exact_tests`… pero **`deck` SÍ entra**, por `run_global_deep.py`, que `run_campaign.sh`
    invoca. No lo vi porque el descubrimiento no seguía los shells anidados (H1), de modo que un
    defecto del sello estaba sosteniendo una afirmación del recibo. La auditoría ciega lo cazó.

    Lo que sigue siendo cierto —y es lo que hace material a #58— es que la campaña **reescribe**
    `model_comparison_*21.csv`, que es lo que `cohort_scan.json` (E2) sella en su procedencia.
    """
    cierre = set(pf.code_closure(pf.runbook_entrypoints()))
    dentro = {"vp_model/deck.py"}
    fuera = {"vp_model/stability.py", "vp_model/universe.py", "vp_model/horizon.py", "vp_model/exact_tests.py"}
    assert dentro <= cierre, "`deck` entra por run_global_deep: si dejara de entrar, revisar el texto"
    assert fuera.isdisjoint(cierre), f"la cadena E entró al cierre ({sorted(fuera & cierre)}): actualizar el texto"

    # …y la dependencia que lo vuelve material: E2 sella los scorecards que la campaña regenera
    scan = json.loads((ROOT / "reports/eval/cohort_scan.json").read_text(encoding="utf-8"))
    sellados = set(scan["provenance"]["scorecards"])
    regenerados = {f"model_comparison_{t}21.csv" for t in ("FAD", "DFF")} | {
        f"model_comparison_EB_{t}21.csv" for t in ("FAD", "DFF")
    }
    assert sellados == regenerados, "E2 sella exactamente los scorecards que el runbook reescribe"


def test_data_inputs_come_from_the_dag_not_from_a_hand_list() -> None:
    dag = yaml.safe_load((ROOT / "dvc.yaml").read_text(encoding="utf-8"))
    datos = pf.data_inputs(dag)
    assert "data/processed/visa_panel_long.csv" in datos and "data/raw" in datos
    esperados = {pf.RAW_SNAPSHOTS}
    for stage in ("scrape", "panel", "bulletins"):
        for out in dag["stages"][stage]["outs"]:
            esperados.add(next(iter(out)) if isinstance(out, dict) else out)
    assert set(datos) == esperados
    # ★ H4: el congelado crudo es DEPENDENCIA del stage `scrape`, no salida de ninguno, así que
    # no aparecía por el DAG y quedaba fuera del sello aunque sea la entrada de todo el pipeline.
    assert pf.RAW_SNAPSHOTS in datos


@NECESITA_EXTRA
def test_governance_inputs_are_anchored_to_the_constant_that_names_them() -> None:
    """Si alguien mueve `champion.MANIFEST`, el sello debe romperse, no seguir sellando el viejo."""
    import importlib

    for rel, modulo, atributo in pf.GOVERNANCE_INPUTS:
        objetivo = getattr(importlib.import_module(modulo), atributo)
        assert Path(objetivo).resolve() == (ROOT / rel).resolve(), f"{modulo}.{atributo} ya no apunta a {rel}"


@NECESITA_EXTRA
def test_seal_fails_closed_when_an_anchor_is_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    """RED: con el ancla movida, sellar ABORTA en vez de emitir un sello engañoso."""
    from vp_model import champion

    monkeypatch.setattr(champion, "MANIFEST", ROOT / "reports" / "governance" / "no_existe.json")
    with pytest.raises(pf.PreflightError, match="ancla"):
        pf.seal()


def test_the_mutable_inputs_that_govern_stages_are_sealed() -> None:
    """★ H4: entradas que gobiernan etapas y estaban FUERA del sello.

    `tuned_params.json` parametriza el pool de la etapa 1 y lo reescribe la 6; `schema.sql` y
    `pipeline/migrations` gobiernan la etapa 0; `consistency_rules.yml` decide la 10 —incluido
    `retro_protocol`—. Ninguna estaba sellada.
    """
    planas = set(pf.GOVERNANCE_PLAIN)
    for rel in (
        "reports/eval/tuned_params.json",
        "schema.sql",
        "pipeline/migrations",
        "tools/consistency_rules.yml",
    ):
        assert rel in planas, f"{rel} gobierna una etapa y debe estar sellada"


def test_the_sealed_locks_are_the_ones_each_interpreter_really_uses() -> None:
    """★ H4: `locks/runtime.txt` no gobierna a `ante` ni a `ante_nf`; cada perfil tiene el suyo."""
    venvs = {v for v, _ in pf.INTERPRETER_LOCKS}
    assert venvs == {"ante", "ante_nf"}, "los dos intérpretes que el runbook invoca"
    for _venv, lock in pf.INTERPRETER_LOCKS:
        assert (ROOT / lock).is_file(), f"el lock declarado no existe: {lock}"


# --------------------------------------------------------------- protocolo
def test_protocol_is_read_from_live_authorities() -> None:
    from vp_model import config, stability

    p = pf.reconcile_protocol()
    assert p["gap_policy"] == "locf_causal", "la rejilla de modelado es LOCF causal (F1)"
    assert p["cohorts"]["rule_version"] == stability.RULE_VERSION
    assert p["cohorts"]["names"] == sorted(stability.COHORTES)
    assert p["run_metadata"]["walkforward"]["holdout"] == config.HOLDOUT
    assert p["horizons"] == list(config.HORIZONS)
    assert p["mask_covariates"], "las máscaras MNAR son parte del protocolo y deben sellarse"


def test_protocol_drops_the_volatile_fields_so_two_preflights_agree() -> None:
    """Un sello que cambie en cada invocación no sirve para comparar contra el recibo."""
    volatiles = {"git_sha", "git_dirty", "timestamp", "run_id", "campaign_id"}
    p = pf.reconcile_protocol()
    assert not (volatiles & set(p["run_metadata"])), "los campos volátiles no pueden entrar al protocolo"
    assert pf.reconcile_protocol() == p, "dos reconciliaciones seguidas deben coincidir"


def test_the_frozen_cohort_deck_is_summarised_by_identity_not_by_result() -> None:
    """Del deck congelado se sella QUÉ recetas existen, nunca cómo les fue."""
    p = pf.reconcile_protocol()["cohort_deck"]
    assert p["primarias"] == ["deepar-legacy", "deepar-robust", "deepar-robust-levels"]
    assert "control-bitcn" in p["controles"]
    assert p["device"] == "cpu" and p["seeds"] == [1]
    plano = json.dumps(p)
    for prohibido in ("mase", "bate_naive1", "p_value", "winner"):
        assert prohibido not in plano.lower(), f"el resumen del deck no puede traer resultados ({prohibido})"


# --------------------------------------------------------------- ensayo del mecanismo
def _bloque_transaccion() -> str:
    guion = (ROOT / "experiments" / "run_rederivation.sh").read_text(encoding="utf-8")
    ini = guion.index("# ── Transacción de campaña")
    fin = guion.index("trap 'exit 143' TERM") + len("trap 'exit 143' TERM")
    return guion[ini:fin]


def _ensayo(tmp_path: Path, desenlace: str) -> tuple[int, dict | None]:
    """Ejecuta el cableado REAL del runbook con un desenlace sintético. No calcula nada."""
    txn = tmp_path / "campaign.json"
    cola = {
        "ok": "txn compute --input-gate passed --output-gate passed --consistency passed || exit 7\nexit 0\n",
        "req_fail": (
            'txn fail --if-open --stage "etapas obligatorias" --exit-code 1 '
            '--reason "1 etapa(s) obligatoria(s) rota(s)" >&2\nexit 1\n'
        ),
        "consistencia": (
            'txn fail --if-open --stage "consistencia" --exit-code 2 '
            '--reason "cómputo completo; las cifras cambiaron y faltan por propagar (regla #0)" >&2\nexit 2\n'
        ),
        "muerte": "kill -9 $$\n",
    }[desenlace]
    guion = tmp_path / "ensayo.sh"
    guion.write_text(
        "#!/bin/bash\nset -uo pipefail\n"
        f'ANTE="{sys.executable}"\nCAMPAIGN_ID="ensayo_m74a"\nCAMPAIGN_SHA="{"a" * 40}"\n'
        f'CAMPAIGN_DIRTY="false"\nCAMPAIGN_TXN="{txn}"\n' + _bloque_transaccion() + "\n" + cola,
        encoding="utf-8",
    )
    fin = subprocess.run(
        ["bash", str(guion)], cwd=ROOT, capture_output=True, text=True, timeout=600,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )  # fmt: skip
    return fin.returncode, cs.read(txn)


@pytest.mark.parametrize(
    "desenlace,rc,estado,razon",
    [
        ("ok", 0, "computed", None),
        ("req_fail", 1, "failed", "obligatoria"),
        ("consistencia", 2, "failed", "propagar"),
    ],
)
def test_hermetic_rehearsal_of_the_three_outcomes(tmp_path: Path, desenlace, rc, estado, razon) -> None:
    """Los tres desenlaces del runbook, con su cableado real, en un temporal."""
    codigo, obj = _ensayo(tmp_path, desenlace)
    assert codigo == rc
    assert obj is not None and obj["status"] == estado
    if razon:
        assert razon in obj["reason"]


def test_hermetic_rehearsal_only_opens_publication_after_human_validation(tmp_path: Path) -> None:
    """El camino completo: ni siquiera `computed` autoriza publicar; sólo `validated` lo hace."""
    from tools import campaign_txn as txn

    codigo, obj = _ensayo(tmp_path, "ok")
    ruta = tmp_path / "campaign.json"
    assert codigo == 0 and obj is not None and obj["status"] == "computed"
    assert not txn.publishable(ruta)[0], "`computed` NO puede autorizar publicar"

    recibo = tmp_path / "recibo.md"
    recibo.write_text("revisión humana del ensayo\n", encoding="utf-8")
    cs.mark_validated(
        ruta,
        validation_receipt_sha256=txn.sha256_file(recibo),
        validation_receipt_path=str(recibo),
        reviewed_by="ensayo M74-A",
        validated_at=txn.now_rfc3339(),
        decision="ensayo, no publica nada",
    )
    assert txn.publishable(ruta)[0], "sólo tras la validación humana se abre la publicación"


def test_the_runbook_refuses_to_start_on_a_dirty_tree() -> None:
    """La guarda de identidad: una campaña oficial exige árbol limpio, y lo dice el guion."""
    guion = (ROOT / "experiments" / "run_rederivation.sh").read_text(encoding="utf-8")
    assert "tree_dirty" in guion and "ALLOW_DIRTY" in guion
    # y ALLOW_DIRTY sin CAMPAIGN_DIAGNOSTIC aborta: una campaña sucia no puede pasar por oficial
    assert "CAMPAIGN_DIAGNOSTIC" in guion and "exit 6" in guion
