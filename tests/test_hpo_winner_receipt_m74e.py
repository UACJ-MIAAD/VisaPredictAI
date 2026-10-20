"""M74-E · La ganadora del HPO se acredita antes de entrenar, y la finalización no reoptimiza.

Dos defectos reales que la campaña `rederiv_1022c9d_20260911T212150` sacó a la luz tras 10 h:

1. `save_finalists_deep.py` importaba `_auto_config`, **renombrado en `0a9ebcc`**, y el fallo sólo
   aparecía al ejecutar la campaña entera: ninguna prueba lo cubría porque el archivo importa
   `neuralforecast`, que vive sólo en el perfil `ante_nf`.
2. El sustituto natural, `_base_config`, **no es equivalente**: le falta el espacio arquitectónico
   de BiTCN (`hidden_size`, `dropout`). Arreglar el import «bien» habría reentrenado el finalista
   con una búsqueda distinta de la que seleccionó la receta — un modelo que no es el evaluado, con
   aspecto de serlo.

La enmienda §8.6 decide que **se busca una vez y la finalización consume**. Aquí se prueba que el
consumidor no puede saltarse la acreditación, y que una ganadora de otra campaña o de otro entorno
**no llega al builder**.

Todo es stdlib y temporales: no entrena nada, no toca `reports/` ni `data/`.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "experiments"))

from hpo_winner_receipt import (  # noqa: E402
    ACCELERATOR,
    N_TRIALS,
    SEARCH_SEED,
    SEARCH_SPACE,
    WinnerReceiptError,
    build_finalist,
    campaign_identity,
    load_and_accredit,
    panel_fingerprint,
    receipt_path,
    sealed_panel_sha256,
    sha256_file,
    write_receipt,
)

CAMPANA = "rederiv_abc1234_20260913T000000"
PANEL_CSV = "country,category,table,value\nmexico,EB2,FAD,1\n"


def _escenario(tmp_path: Path, *, status: str = "running", panel_txt: str = PANEL_CSV) -> tuple[Path, str]:
    """Una campaña sintética COMPLETA: repositorio git, panel en disco y transacción sellada.

    El acreditador ya no acepta la identidad por argumento —la lee de aquí—, así que las pruebas
    tienen que construir el mundo que va a leer. Eso es justamente lo que hace que no puedan
    mentirle: exportar una variable de entorno ya no basta.
    """
    import subprocess

    raiz = tmp_path / "repo"
    (raiz / "reports" / "campaign").mkdir(parents=True)
    (raiz / "data" / "processed").mkdir(parents=True)
    panel = raiz / "data" / "processed" / "visa_panel_long.csv"
    panel.write_text(panel_txt, encoding="utf-8")

    def git(*a: str) -> str:
        fin = subprocess.run(["git", *a], cwd=raiz, capture_output=True, text=True, check=True)
        return fin.stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "ensayo@local")
    git("config", "user.name", "ensayo")
    git("add", "-A")
    git("commit", "-qm", "panel")
    head = git("rev-parse", "HEAD")

    (raiz / "reports" / "campaign" / "campaign.json").write_text(
        json.dumps(
            {
                "campaign_id": CAMPANA,
                "source_git_sha": head,
                "panel_sha256": panel_fingerprint(panel),
                "status": status,
            }
        ),
        encoding="utf-8",
    )
    return raiz, head


def _ganadora(raiz: Path, **cambios) -> Path:
    """Una configuración ganadora con la forma REAL que `_dump_best_config` persiste."""
    cfg = {
        "input_size": 18,
        "learning_rate": 0.000117,
        "scaler_type": "robust",
        "max_steps": 2000,
        "early_stop_patience_steps": 10,
        "val_check_steps": 25,
        "logger": False,
        "accelerator": ACCELERATOR,
        "hidden_size": 16,
        "dropout": 0.0111,
    }
    cfg.update(cambios)
    p = raiz / "reports" / "campaign" / "hpo_deep_best_FAD_AutoBiTCN.json"
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return p


def _sellar(raiz: Path, ganadora: Path, **cambios) -> Path:
    ident = campaign_identity(raiz)
    datos = {
        "campaign_id": ident["campaign_id"],
        "code_sha": ident["source_git_sha"],
        "panel_sha256": ident["panel_sha256"],
        "table": "FAD",
        "model": "AutoBiTCN",
        "accelerator_in_artifact": ACCELERATOR,
    }
    datos.update(cambios)
    return write_receipt(ganadora, **datos)


def _acreditar(raiz: Path, ganadora: Path, **cambios):
    kw = {"root": raiz, "table": "FAD", "model": "AutoBiTCN"}
    kw.update(cambios)
    return load_and_accredit(ganadora, **kw)


@pytest.fixture(autouse=True)
def entorno_oficial(monkeypatch: pytest.MonkeyPatch):
    """§8: una campaña oficial declara su acelerador. El acreditador lo LEE de aquí."""
    monkeypatch.setenv("VP_DEEP_ACCEL", ACCELERATOR)


# ═════════════════════════════════ 1 · el control benigno: lo legítimo SÍ pasa
def test_a_sealed_winner_from_this_campaign_accredits_and_reaches_the_builder(tmp_path: Path) -> None:
    """La mitad que da sentido a los REDs: sin esto, «rechaza todo» pasaría por «acredita bien»."""
    raiz, _ = _escenario(tmp_path)
    g = _ganadora(raiz)
    _sellar(raiz, g)
    cfg = _acreditar(raiz, g)
    assert cfg["hidden_size"] == 16, "el espacio arquitectónico de BiTCN debe llegar al builder"
    assert cfg["dropout"] == pytest.approx(0.0111)
    assert cfg["accelerator"] == ACCELERATOR


# ═════════════════════════════════ 2 · el acelerador: tres acreditaciones separadas
def test_an_mps_winner_is_refused_before_building_the_model(tmp_path: Path) -> None:
    """★ La ganadora de agosto declara `accelerator: "mps"`.

    ⚠️ El motivo del rechazo NO es que fuera a ejecutarse en MPS: `_build_from_config` escribe
    `"accelerator": _accelerator()` **después** de desempaquetar la config, así que el valor
    efectivo ya sobrescribe al almacenado. Se rechaza porque es **evidencia de otra campaña o de
    otro entorno**: la sobrescritura protege la ejecución, el rechazo protege la procedencia.
    """
    raiz, _ = _escenario(tmp_path)
    g = _ganadora(raiz, accelerator="mps")
    _sellar(raiz, g, accelerator_in_artifact="mps")
    with pytest.raises(WinnerReceiptError, match="procedencia"):
        _acreditar(raiz, g)


def test_the_three_accelerator_accreditations_are_independent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cada una falla por su cuenta: ninguna cubre a las otras dos."""
    # (a) el artefacto dice mps, el recibo dice cpu → discrepan entre sí
    raiz, _ = _escenario(tmp_path)
    g = _ganadora(raiz, accelerator="mps")
    _sellar(raiz, g, accelerator_in_artifact=ACCELERATOR)
    with pytest.raises(WinnerReceiptError, match="accelerator"):
        _acreditar(raiz, g)
    # (b) artefacto y recibo coherentes en cpu, pero el ENTORNO oficial no está fijado
    g2 = _ganadora(raiz)
    _sellar(raiz, g2)
    monkeypatch.setenv("VP_DEEP_ACCEL", "mps")
    with pytest.raises(WinnerReceiptError, match="VP_DEEP_ACCEL"):
        _acreditar(raiz, g2)
    monkeypatch.delenv("VP_DEEP_ACCEL")
    with pytest.raises(WinnerReceiptError, match="VP_DEEP_ACCEL"):
        _acreditar(raiz, g2)


# ═════════════════════════════════ 3 · procedencia: ausente, mezclada, antigua, manipulada
def test_a_winner_without_a_receipt_does_not_accredit(tmp_path: Path) -> None:
    """Hoy las ganadoras de agosto no llevan recibo: no pueden pasar por las de esta campaña."""
    raiz, _ = _escenario(tmp_path)
    g = _ganadora(raiz)
    assert not receipt_path(g).exists()
    with pytest.raises(WinnerReceiptError, match="no lleva recibo"):
        _acreditar(raiz, g)


@pytest.mark.parametrize(
    "campo,valor,patron",
    [
        ("campaign_id", "rederiv_otra_20260101T000000", "otra corrida"),
        ("code_sha", "9999999", "otra corrida"),
        ("panel_sha256", "sha256:" + "9" * 64, "otra corrida"),
        ("table", "DFF", "otra corrida"),
        ("model", "AutoTiDE", "otra corrida"),
    ],
)
def test_a_winner_from_another_campaign_or_vintage_does_not_accredit(
    tmp_path: Path, campo: str, valor: str, patron: str
) -> None:
    """★ Mezclar añadas es exactamente lo que contaminó la etapa [4] de la corrida fallida."""
    raiz, _ = _escenario(tmp_path)
    g = _ganadora(raiz)
    _sellar(raiz, g, **{campo: valor})
    with pytest.raises(WinnerReceiptError, match=patron):
        _acreditar(raiz, g)


def test_a_tampered_winner_does_not_accredit(tmp_path: Path) -> None:
    """El hash acredita que el archivo no cambió DESDE el sellado."""
    raiz, _ = _escenario(tmp_path)
    g = _ganadora(raiz)
    _sellar(raiz, g)
    _ganadora(raiz, hidden_size=32)  # se reescribe el mismo archivo
    with pytest.raises(WinnerReceiptError, match="cambió desde el sellado"):
        _acreditar(raiz, g)


@pytest.mark.parametrize(
    "campo,valor",
    [("search_space", "_base_config"), ("n_trials", 15), ("search_seed", 7)],
)
def test_a_winner_from_a_different_procedure_does_not_accredit(tmp_path: Path, campo: str, valor) -> None:
    """★ `_base_config` y 15 trials son exactamente lo que la finalización hacía antes."""
    raiz, _ = _escenario(tmp_path)
    g = _ganadora(raiz)
    acta_path = _sellar(raiz, g)
    acta = json.loads(acta_path.read_text(encoding="utf-8"))
    acta[campo] = valor
    acta_path.write_text(json.dumps(acta), encoding="utf-8")
    with pytest.raises(WinnerReceiptError, match="protocolo congelado exige"):
        _acreditar(raiz, g)


def test_a_receipt_with_duplicate_or_wrong_keys_does_not_accredit(tmp_path: Path) -> None:
    raiz, _ = _escenario(tmp_path)
    g = _ganadora(raiz)
    acta_path = _sellar(raiz, g)
    base = json.loads(acta_path.read_text(encoding="utf-8"))

    acta_path.write_text(json.dumps(base)[:-1] + ', "extra": 1}', encoding="utf-8")
    with pytest.raises(WinnerReceiptError, match="sobran"):
        _acreditar(raiz, g)

    sin_clave = {k: v for k, v in base.items() if k != "search_seed"}
    acta_path.write_text(json.dumps(sin_clave), encoding="utf-8")
    with pytest.raises(WinnerReceiptError, match="faltan"):
        _acreditar(raiz, g)

    crudo = json.dumps(base)[:-1] + f', "campaign_id": "{CAMPANA}"}}'
    acta_path.write_text(crudo, encoding="utf-8")
    with pytest.raises(WinnerReceiptError, match="duplicadas"):
        _acreditar(raiz, g)


# ═════════════════════════════════ 4 · el panel se toma de la transacción sellada
def test_the_identity_comes_from_the_sealed_transaction_and_requires_running(tmp_path: Path) -> None:
    """⚠️ `_identity()["panel_hash"]` es un md5[:12]; llamarlo `panel_sha256` habría sido mentir."""
    with pytest.raises(WinnerReceiptError, match="no hay transacción"):
        campaign_identity(tmp_path)

    raiz, head = _escenario(tmp_path)
    ident = campaign_identity(raiz)
    assert ident["source_git_sha"] == head
    assert ident["panel_sha256"].startswith("sha256:")
    assert sealed_panel_sha256(raiz) == ident["panel_sha256"]


@pytest.mark.parametrize("estado", ["computed", "validated", "failed", "published"])
def test_a_campaign_that_is_not_running_cannot_seal_or_consume_a_winner(tmp_path: Path, estado: str) -> None:
    """★ `running` EXACTO: una campaña terminada no puede producir ni consumir ganadoras."""
    raiz, _ = _escenario(tmp_path, status=estado)
    with pytest.raises(WinnerReceiptError, match="no hay campaña en curso"):
        campaign_identity(raiz)


def test_the_triple_equality_needs_the_third_term(tmp_path: Path) -> None:
    """★ Dos declaraciones coherentes entre sí pueden estar ambas equivocadas sobre el mundo.

    El recibo y la transacción pueden decir lo mismo y ser falsos: el tercer término —el panel que
    hay en disco y el HEAD vivo— es el que los ancla.
    """
    raiz, _ = _escenario(tmp_path)
    g = _ganadora(raiz)
    _sellar(raiz, g)
    assert _acreditar(raiz, g)["hidden_size"] == 16  # control: los tres coinciden

    # el panel cambia bajo la campaña: recibo y transacción SIGUEN coincidiendo entre sí
    (raiz / "data" / "processed" / "visa_panel_long.csv").write_text("country,x\nmexico,9\n", encoding="utf-8")
    with pytest.raises(WinnerReceiptError, match="el panel cambió bajo la campaña"):
        _acreditar(raiz, g)


def test_a_moved_head_breaks_the_code_accreditation(tmp_path: Path) -> None:
    """El árbol no puede moverse durante la campaña: el código sellado deja de ser el vivo."""
    import subprocess

    raiz, _ = _escenario(tmp_path)
    g = _ganadora(raiz)
    _sellar(raiz, g)
    (raiz / "otro.txt").write_text("cambio posterior\n", encoding="utf-8")
    for a in (["add", "-A"], ["commit", "-qm", "movido"]):
        subprocess.run(["git", *a], cwd=raiz, capture_output=True, check=True)
    with pytest.raises(WinnerReceiptError, match="el árbol se movió"):
        _acreditar(raiz, g)


# ═════════════════════════════════ 5 · el finalizador ya no reoptimiza (por AST, no por texto)
FINALIZADOR = RAIZ / "experiments" / "save_finalists_deep.py"


def _arbol() -> ast.Module:
    return ast.parse(FINALIZADOR.read_text(encoding="utf-8"))


def test_the_finaliser_no_longer_imports_autobitcn_or_the_dead_symbol() -> None:
    """★ RED pedido: ni `AutoBiTCN` de neuralforecast.auto, ni `_auto_config`, ni Optuna."""
    importados: set[str] = set()
    modulos: set[str] = set()
    for nodo in ast.walk(_arbol()):
        if isinstance(nodo, ast.ImportFrom):
            modulos.add(nodo.module or "")
            importados.update(a.name for a in nodo.names)
        elif isinstance(nodo, ast.Import):
            modulos.update(a.name for a in nodo.names)
    assert "_auto_config" not in importados, "el símbolo muerto no puede volver"
    assert "AutoBiTCN" not in importados, "la clase Auto* abre una búsqueda: la finalización no busca"
    assert not {m for m in modulos if "optuna" in m.lower()}, "la finalización no abre Optuna"
    assert not {m for m in modulos if m.endswith("neuralforecast.auto")}, "ni el paquete Auto*"
    assert "_optuna_sampler" not in importados


def test_the_finaliser_accredits_before_it_builds() -> None:
    """★ El orden ES la garantía: acreditar después de construir no evitaría entrenar de más."""
    fuente = FINALIZADOR.read_text(encoding="utf-8")
    arbol = _arbol()
    llamadas = {
        n.func.id: n.lineno for n in ast.walk(arbol) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "load_and_accredit" in llamadas, "el finalizador debe acreditar la ganadora"
    # el builder de AutoBiTCN instancia la clase concreta BiTCN (§8.6.1)
    linea_builder = fuente.index('builders["AutoBiTCN"]')
    linea_builder = fuente[:linea_builder].count("\n") + 1
    assert llamadas["load_and_accredit"] < linea_builder, (
        "la acreditación tiene que ocurrir ANTES de construir el modelo"
    )
    assert "build_finalist(BiTCN," in fuente, "la clase instanciada es BiTCN, no AutoBiTCN (§8.6.1)"


def test_the_searcher_seals_a_receipt_for_every_winner_it_writes() -> None:
    """Quien busca sella; sin eso, el consumidor no tendría qué acreditar."""
    fuente = (RAIZ / "experiments" / "run_global_deep.py").read_text(encoding="utf-8")
    assert "write_receipt(" in fuente
    i_json = fuente.index("os.replace(_tmp, ganadora)")
    i_recibo = fuente.index("write_receipt(")
    assert i_json < i_recibo, "el recibo se sella DESPUÉS de promover la ganadora (hashea su contenido)"
    assert "_tmp.write_text(" in fuente, "la ganadora se escribe en staging y se promueve con os.replace"
    # ★ y el fallo del dump YA NO es un WARN
    assert "WARN sin dump de config" not in fuente, "un dump fallido no puede seguir siendo un aviso"
    assert "no se pudo sellar la config ganadora ni su recibo" in fuente


def test_the_receipt_declares_the_frozen_procedure() -> None:
    """Los tres valores de §8.6.1 no son parámetros: son el procedimiento declarado."""
    assert (SEARCH_SPACE, N_TRIALS, SEARCH_SEED) == ("_cfg_bitcn", 40, 1)


def test_sha256_file_reads_the_real_bytes(tmp_path: Path) -> None:
    """Control barato: el hash del recibo es el del archivo, no el de su ruta."""
    import hashlib

    p = tmp_path / "x.json"
    p.write_bytes(b'{"a": 1}')
    assert sha256_file(p) == hashlib.sha256(b'{"a": 1}').hexdigest()


# ═════════════════════════════════ 6 · los dos huecos que la revisión cerró
def test_a_receipt_sharing_only_the_short_prefix_does_not_accredit(tmp_path: Path) -> None:
    """★ Siete caracteres son un parecido, no una identidad.

    El recibo comparte los primeros 7 con la campaña y difiere en los 33 restantes. Con el SHA
    truncado esto pasaba; con los 40 completos, no.
    """
    raiz, head = _escenario(tmp_path)
    g = _ganadora(raiz)
    impostor = head[:7] + ("0" * 33 if head[7:] != "0" * 33 else "1" * 33)
    assert impostor[:7] == head[:7] and impostor != head
    _sellar(raiz, g, code_sha=impostor)
    with pytest.raises(WinnerReceiptError, match="otra corrida"):
        _acreditar(raiz, g)


def test_the_constructor_really_receives_cpu(tmp_path: Path) -> None:
    """★ Espía sobre el constructor: qué llega DE VERDAD, no qué se pretendía pasar.

    Las tres acreditaciones quedan separadas y verificadas:
    artefacto ganador = cpu · entorno oficial = cpu · **constructor real = cpu**.
    """
    raiz, _ = _escenario(tmp_path)
    g = _ganadora(raiz)
    _sellar(raiz, g)
    cfg = _acreditar(raiz, g)

    recibido: dict = {}

    class BiTCNEspia:
        def __init__(self, **kw):
            recibido.update(kw)

    build_finalist(BiTCNEspia, cfg)
    assert recibido["accelerator"] == "cpu", "el constructor debe recibir cpu, venga lo que venga"
    assert recibido["random_seed"] == SEARCH_SEED
    assert recibido["hidden_size"] == 16, "el espacio arquitectónico acreditado llega intacto"
    assert "loss" not in recibido and "h" in recibido, "CONFIG_DROP filtra lo que no es kwarg"


def test_the_constructor_gets_cpu_even_if_the_winner_said_otherwise(tmp_path: Path) -> None:
    """Control del control: aunque un cfg trajera otro acelerador, el builder impone cpu.

    La acreditación ya habría rechazado ese cfg; esto prueba la segunda capa, no la sustituye.
    """
    recibido: dict = {}

    class BiTCNEspia:
        def __init__(self, **kw):
            recibido.update(kw)

    build_finalist(BiTCNEspia, {"accelerator": "mps", "hidden_size": 8})
    assert recibido["accelerator"] == "cpu"


def test_the_finaliser_does_not_consult_the_machine_accelerator() -> None:
    """El finalizador no puede volver a preguntarle a la máquina qué acelerador usar."""
    fuente = FINALIZADOR.read_text(encoding="utf-8")
    vivo = "\n".join(ln for ln in fuente.splitlines() if not ln.lstrip().startswith("#"))
    assert "_accelerator()" not in vivo, "preferiría MPS si la máquina lo tiene"
    assert "build_finalist(" in vivo, "construye desde la config acreditada"
