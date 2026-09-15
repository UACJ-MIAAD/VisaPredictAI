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


def test_the_reader_discards_what_is_only_named_in_a_comment(tmp_path: Path) -> None:
    """RED del descarte, sobre un runbook SEMBRADO (H19).

    La versión anterior comprobaba que el runbook real tuviera *algún* comentario con
    `experiments/` dentro. Eso pasa aunque el lector no descarte nada: no discriminaba. Aquí el
    guion nombra tres entrypoints **sólo** en comentarios y ninguno puede acabar en el sello.
    """
    sembrado = tmp_path / "runbook_sembrado.sh"
    sembrado.write_text(
        "#!/bin/bash\n"
        "# receta vieja: $ANTE experiments/no_debe_sellarse.py\n"
        "# ni por módulo: $ANTE -m vp_model.no_debe_sellarse\n"
        "$ANTE experiments/run_champion_challenger.py --pool F1  # tools/tampoco_aqui.py\n"
        "bash experiments/run_campaign.sh\n",
        encoding="utf-8",
    )
    eps = pf.runbook_entrypoints(sembrado)
    assert "experiments/run_champion_challenger.py" in eps["scripts"], "la llamada viva sí se sella"
    assert "experiments/run_campaign.sh" in eps["shell"]
    # y la recursión se sigue de verdad: esto NO está en el guion sembrado, sólo en el anidado
    assert "experiments/run_global_deep.py" in eps["scripts"]
    for comentado in ("experiments/no_debe_sellarse.py", "tools/tampoco_aqui.py"):
        assert comentado not in eps["scripts"], f"{comentado} sólo aparece comentado"
    assert "vp_model.no_debe_sellarse" not in eps["modules"]


def test_code_closure_is_transitive() -> None:
    cierre = set(pf.code_closure(pf.runbook_entrypoints()))
    assert "experiments/run_champion_challenger.py" in cierre  # entrypoint directo
    # alcanzados SÓLO por transitividad: ninguno lo invoca el runbook
    for indirecto in ("vp_model/config.py", "vp_model/metrics.py", "vp_model/scale.py", "vp_model/walkforward.py"):
        assert indirecto in cierre, f"{indirecto} debería entrar por cierre transitivo"
    # los guiones de shell y el propio runbook también se sellan
    assert "experiments/run_campaign.sh" in cierre and "experiments/run_rederivation.sh" in cierre


def test_the_campaign_now_re_derives_the_cohort_chain_inside_the_transaction() -> None:
    """★ #58 opción A, ejecutada: la cadena E entra al runbook y, por tanto, al sello.

    Historia de esta prueba, porque importa: en M74-A afirmé que «el runbook no alcanza la cadena
    E». Era **falso en un módulo** —`deck` entraba por `run_global_deep`— y no lo vi porque el
    descubrimiento no seguía los shells anidados: un defecto del sello sostenía una afirmación del
    recibo. La auditoría ciega lo cazó (H1, H19). Corregido el descubrimiento y tomada la decisión
    A, ahora la cadena se re-deriva **dentro de la misma transacción**, que es lo que exige que
    la campaña reescriba los scorecards que E2 sella.
    """
    eps = pf.runbook_entrypoints()
    cierre = set(pf.code_closure(eps))
    for etapa in (
        "experiments/build_cohorts.py",
        "experiments/scan_cohorts.py",
        "experiments/run_e3_campaign.py",
        "experiments/score_e3_campaign.py",
        "experiments/run_e4_router.py",
        "experiments/build_e5_facts.py",
        "experiments/build_horizon_facts.py",
    ):
        assert etapa in eps["scripts"], f"{etapa} debe correr dentro de la transacción (#58 opción A)"
    for autoridad in ("vp_model/stability.py", "vp_model/deck.py", "vp_model/horizon.py", "vp_model/exact_tests.py"):
        assert autoridad in cierre, f"{autoridad} gobierna la cadena E y debe quedar sellado"

    # la dependencia que lo volvía material sigue ahí, y ahora está cubierta
    scan = json.loads((ROOT / "reports/eval/cohort_scan.json").read_text(encoding="utf-8"))
    sellados = set(scan["provenance"]["scorecards"])
    regenerados = {f"model_comparison_{t}21.csv" for t in ("FAD", "DFF")} | {
        f"model_comparison_EB_{t}21.csv" for t in ("FAD", "DFF")
    }
    assert sellados == regenerados


def test_the_e3_lane_plan_is_derived_from_the_frozen_deck() -> None:
    """Los 42 lanes no se escriben a mano: salen del deck, por su única puerta."""
    import importlib

    orquestador = importlib.import_module("experiments.run_e3_campaign")
    lanes = orquestador.plan()
    assert len(lanes) == 42, f"el deck deriva 42 lanes, no {len(lanes)}"
    assert sum(1 for x in lanes if x.role == "primary") == 18
    assert sum(1 for x in lanes if x.role == "control") == 24
    # cada receta corre con el intérprete que ELLA declara: el control GBM va al venv del producto
    gbm = [x for x in lanes if x.runner == "run_global_gbm.py"]
    assert gbm and all(x.command[0] == "ante/bin/python" for x in gbm)
    deep = [x for x in lanes if x.runner == "run_global_deep.py"]
    assert deep and all(x.command[0] == "ante_nf/bin/python" for x in deep)
    # y el plan es determinista
    assert [x.command for x in orquestador.plan()] == [x.command for x in lanes]


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
    """★ H4: entradas que gobiernan etapas y estaban FUERA del sello."""
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
    assert venvs == {"ante", "ante_nf"}
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
    # ⚠️ El ancla es la ÚLTIMA línea del cableado (el trap de SIGHUP), no una intermedia. Anclar a
    # `trap 'exit 143' TERM` hizo que estas pruebas se rompieran en cuanto M74-E lo sustituyó por
    # `campaign_stop SIGTERM 143`, y peor: si el ancla intermedia hubiera sobrevivido, el ensayo
    # habría ejecutado un bloque TRUNCADO sin avisar. El final del bloque es el final del bloque.
    ancla = "trap 'exit 129' HUP"
    fin = guion.index(ancla) + len(ancla)
    return guion[ini:fin]


def _ensayo(tmp_path: Path, desenlace: str, manifiesto: Path | None = None) -> tuple[int, dict | None]:
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
    ident = json.loads(manifiesto.read_text(encoding="utf-8")) if manifiesto else {}  # ★ M74-E-R13
    guion = tmp_path / "ensayo.sh"
    guion.write_text(
        "#!/bin/bash\nset -uo pipefail\n"
        f'ANTE="{sys.executable}"\nCAMPAIGN_ID="{ident.get("campaign_id", "ensayo_m74a")}"\nCAMPAIGN_SHA="{"a" * 40}"\n'
        f'CAMPAIGN_DIRTY="false"\nCAMPAIGN_TXN="{txn}"\nPREFLIGHT_SHA256="{ident.get("preflight_sha256", "e" * 64)}"\n'
        + _bloque_transaccion()
        + "\n"
        + cola,
        encoding="utf-8",
    )
    panel = tmp_path / "panel_de_ensayo.csv"
    panel.write_text("serie,mes,valor\n", encoding="utf-8")
    fin = subprocess.run(
        ["bash", str(guion)], cwd=ROOT, capture_output=True, text=True, timeout=600,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT),
            # ★ H29: el bloque ensayado es el REAL, y el real archiva en `reports/campaign/` y
            # sella el panel de `data/`. Mientras no escriba no pasa nada, pero una prueba que
            # apunta al árbol vivo es un accidente esperando: aquí van a un temporal.
            "CAMPAIGN_TXN_ARCHIVE": str(tmp_path / "transactions"),
            "CAMPAIGN_TXN_PANEL": str(panel),
        },
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


def test_hermetic_rehearsal_only_opens_publication_after_human_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, campana_sellada
) -> None:
    """El camino completo: ni siquiera `computed` autoriza publicar; sólo `validated` lo hace."""
    from tools import campaign_txn as txn

    manifiesto = campana_sellada(tmp_path / "repo")  # ★ M74-E-R13: publicar exige el manifiesto acreditado
    codigo, obj = _ensayo(tmp_path, "ok", manifiesto)
    ruta = tmp_path / "campaign.json"
    assert codigo == 0 and obj is not None and obj["status"] == "computed"
    assert not txn.publishable(ruta, manifest=manifiesto)[0], "`computed` NO puede autorizar publicar"

    # ★ M74-B-R1: la validación se acredita por el camino REAL —recibo de esquema cerrado ligado a
    # esta campaña— y no llamando a `mark_validated` con un `.md` cualquiera y un revisor tecleado,
    # que es lo que hacía esta prueba y lo que la revisión del autor señaló como no acreditado.
    estado = cs.read(ruta) or {}
    recibo = tmp_path / "recibo.json"
    recibo.write_text(
        json.dumps(
            {
                "schema": txn.RECEIPT_SCHEMA,
                "campaign_id": estado["campaign_id"],
                "source_git_sha": estado["source_git_sha"],
                "panel_sha256": estado["panel_sha256"],
                "input_seal_sha256": estado["input_seal_sha256"],
                "reviewed_by": "Javier Rebull",
                "decision": "aprobada",
                "reviewed_at": txn.now_rfc3339(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(txn, "_run_consistency", lambda: (True, ""))
    assert txn.main(["--path", str(ruta), "validate", "--receipt", str(recibo)]) == txn.EXIT_OK
    assert txn.publishable(ruta, manifest=manifiesto)[0], (
        "sólo tras la validación humana acreditada se abre la publicación"
    )


def test_a_killed_campaign_stays_open_and_blocks_the_next_one(tmp_path: Path) -> None:
    """★ H19: `"muerte"` existía en el ensayo y **nunca se ejecutaba**.

    Es el desenlace que ningún trap puede atrapar: `SIGKILL` no da al proceso la oportunidad de
    marcar `failed`. La propiedad que importa no es que se registre el fallo —no puede—, sino que
    el estado **no mienta**: queda `running`, y la siguiente campaña se niega a arrancar encima.
    Eso es exactamente para lo que existe la máquina.
    """
    codigo, obj = _ensayo(tmp_path, "muerte")
    assert codigo in (-9, 137), f"el ensayo debía morir por SIGKILL, salió {codigo}"
    assert obj is not None, "la transacción sellada al abrir debe sobrevivir a la muerte"
    assert obj["status"] == "running", f"un proceso muerto no puede dejar '{obj['status']}'"
    assert obj["campaign_id"] == "ensayo_m74a"

    # y la siguiente no arranca: `txn archive` se niega a apartar una campaña abierta
    codigo2, obj2 = _ensayo(tmp_path, "ok")
    assert codigo2 == 7, "lanzar una campaña sobre otra abierta debe abortar"
    assert obj2 is not None and obj2["status"] == "running", "la campaña muerta no se toca"


def test_the_rehearsal_archives_into_the_fixture_and_never_into_the_repo(tmp_path: Path) -> None:
    """★ H29: el archivado ocurre de verdad, y ocurre en el temporal.

    Antes el ensayo pasaba `--dir reports/campaign/transactions` y `--panel data/…` del árbol
    vivo. Hoy no escribía porque nunca había una campaña terminal que archivar; en cuanto la hay,
    escribe. Se comprueba las dos mitades: que el archivo cae en la fixture y que el directorio
    real del repositorio queda exactamente como estaba.
    """
    real = ROOT / "reports" / "campaign" / "transactions"
    antes = sorted(x.name for x in real.iterdir()) if real.is_dir() else None

    codigo, obj = _ensayo(tmp_path, "req_fail")  # deja la transacción en `failed` (terminal)
    assert codigo == 1 and obj is not None and obj["status"] == "failed"
    codigo2, obj2 = _ensayo(tmp_path, "ok")  # ésta SÍ archiva la anterior
    assert codigo2 == 0 and obj2 is not None and obj2["status"] == "computed"

    archivadas = sorted(x.name for x in (tmp_path / "transactions").iterdir())
    assert archivadas == ["ensayo_m74a.json"], f"la anterior debía archivarse en la fixture: {archivadas}"
    despues = sorted(x.name for x in real.iterdir()) if real.is_dir() else None
    assert despues == antes, "el ensayo no puede escribir en reports/campaign/transactions"


def _bloque_guarda() -> str:
    """El bloque REAL de identidad + árbol limpio, recortado del runbook."""
    guion = (ROOT / "experiments" / "run_rederivation.sh").read_text(encoding="utf-8")
    return guion[guion.index("tree_dirty() {") : guion.index('PREFLIGHT_TMP="$(mktemp')]


def _ejecuta_guarda(tmp_path: Path, ensuciar: str | None, extra: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Corre la guarda contra un repositorio git DE VERDAD, creado en el temporal."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "ensayo@local")
    git("config", "user.name", "ensayo")
    (repo / "producto.py").write_text("x = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "base")
    if ensuciar == "tracked-modificado":
        (repo / "producto.py").write_text("x = 2\n", encoding="utf-8")
    elif ensuciar == "codigo-untracked":
        (repo / "suelto.py").write_text("y = 1\n", encoding="utf-8")
    elif ensuciar == "salida-untracked":
        (repo / "reporte.csv").write_text("a,b\n", encoding="utf-8")

    # ⚠️ El guion vive FUERA del repositorio bajo prueba: dentro, la guarda lo contaba a él
    #    mismo como `.sh` untracked y el control benigno salía rojo. La guarda tenía razón.
    guion = tmp_path / "guarda.sh"
    guion.write_text("#!/bin/bash\nset -uo pipefail\n" + _bloque_guarda(), encoding="utf-8")
    limpio = {k: v for k, v in os.environ.items() if k not in ("ALLOW_DIRTY", "CAMPAIGN_DIAGNOSTIC")}
    return subprocess.run(
        ["bash", str(guion)], cwd=repo, capture_output=True, text=True, timeout=120, env={**limpio, **extra}
    )


@pytest.mark.parametrize(
    "ensuciar,extra,rc,huella",
    [
        (None, {}, 0, "dirty=false"),
        # ★ un output generado NO ensucia: el control benigno es la mitad que da sentido al RED
        ("salida-untracked", {}, 0, "dirty=false"),
        ("tracked-modificado", {}, 1, "tracked-modificado"),
        ("codigo-untracked", {}, 1, "codigo-untracked"),
        # ALLOW_DIRTY salta la guarda, pero una campaña OFICIAL sucia sigue abortando
        ("codigo-untracked", {"ALLOW_DIRTY": "1"}, 6, "CAMPAIGN_DIAGNOSTIC"),
        ("codigo-untracked", {"ALLOW_DIRTY": "1", "CAMPAIGN_DIAGNOSTIC": "1"}, 0, "dirty=true"),
    ],
)
def test_the_dirty_tree_guard_is_executed_not_merely_present(
    tmp_path: Path, ensuciar: str | None, extra: dict[str, str], rc: int, huella: str
) -> None:
    """★ H19: antes esto era `assert "tree_dirty" in guion`, que pasa con la guarda rota.

    Ahora el bloque real corre contra un repositorio git creado en el temporal, con los seis
    desenlaces que distinguen un árbol limpio de uno sucio y una campaña oficial de una
    diagnóstica. El repo vivo no se toca: la guarda nunca se ejecuta contra `ROOT`.
    """
    fin = _ejecuta_guarda(tmp_path, ensuciar, extra)
    assert fin.returncode == rc, f"esperaba exit {rc}, salió {fin.returncode}: {fin.stderr[-400:]}"
    assert huella in (fin.stdout + fin.stderr), f"no aparece la huella {huella!r}"
