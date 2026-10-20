"""M74-E-R3 · la frontera PÚBLICA tenía un atajo que el camino canónico no usaba.

R2 cerró tres ataques contra la sustancia del artefacto, y dejó abierto el más simple de todos:
`read_accredited` admitía `campaign_id`, `code_sha` y `panel_sha256` por parámetro y, con los tres
puestos, **nunca llamaba a `campaign_identity`**. Reproducido por el autor contra `531055d`:

    accepted_without_transaction: 5400
    campaign_json_exists: false

5 400 claves REALES, valores finitos, recibo coherente… con identidades **inventadas** y **sin que
existiera `campaign.json`**. Los nueve consumidores no usaban el atajo, así que el camino canónico
estaba protegido; la frontera pública no.

⚠️ **Y el atajo existía porque mis propias pruebas dependían de él.** Es la forma exacta que este
repositorio ya conocía: un bypass sobrevive porque algo lo usa, y casi siempre es una prueba. Las
de aquí montan una campaña coherente con el repositorio vivo, que es lo que hace la campaña real.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

MODELADO = pytest.mark.skipif(importlib.util.find_spec("darts") is None, reason="extra `model` (darts) ausente")


# ═══════════════════════════════ 1 · el RED exacto que reprodujo el autor
@MODELADO
def test_RED_5400_filas_reales_sin_transaccion_no_se_acreditan(tmp_path: Path, monkeypatch) -> None:
    """★ EL caso, literal: rejilla completa, valores finitos, recibo coherente, cero `campaign.json`."""
    from tests import holdout_fixture as hf
    from vp_model import artifact_receipt as ar
    from vp_model import persist_forecasts as pf

    reports = tmp_path / "reports"
    (reports / "eval").mkdir(parents=True)
    hf.artefacto_completo(reports, "FAD", campaign_id="INVENTADA")  # 5 400 claves REALES
    monkeypatch.setenv("CAMPAIGN_ID", "INVENTADA")
    monkeypatch.setenv("CAMPAIGN_SHA", "z" * 40)

    assert not (reports / "campaign" / "campaign.json").exists(), "el escenario exige que NO haya transacción"

    # ★ El ataque, TAL COMO ERA: pasando la identidad por parámetro. Contra `531055d` esto devolvía
    #   las 5 400 filas; ahora ni siquiera es invocable, que es la única forma de cerrarlo del todo.
    #   ⚠️ Esta es la discriminación real. Llamar sin identidad —la línea de abajo— ya fallaba en
    #   `531055d`, así que por sí sola NO distingue las dos versiones: es el control, no el RED.
    with pytest.raises(TypeError, match="campaign_id"):
        pf.read_accredited(  # type: ignore[call-arg]
            "FAD", reports=reports, campaign_id="INVENTADA", code_sha="z" * 40, panel_sha256="sha256:" + "9" * 64
        )
    with pytest.raises(ar.ReceiptError, match="transacción de campaña"):
        pf.read_accredited("FAD", reports=reports)


PROHIBIDOS = ("campaign_id", "code_sha", "panel_sha256", "pool", "block", "code_root")


def test_la_puerta_publica_no_admite_identidad_por_parametro_AST() -> None:
    """Si se puede pasar la identidad, se puede inventar. La firma es el contrato.

    ⚠️ Por AST y no importando el módulo: `persist_forecasts` arrastra `darts` y el job base sólo
    instala `.[dev]`. Esta comprobación es estructural y barata, así que debe correr en LOS DOS
    jobs — justo la razón por la que no se le pone la guarda del extra.
    """
    import ast

    arbol = ast.parse((RAIZ / "vp_model" / "persist_forecasts.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(arbol) if isinstance(n, ast.FunctionDef) and n.name == "read_accredited")
    nombres = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
    assert nombres == {"table", "reports"}, f"la frontera pública creció: {sorted(nombres)}"
    assert fn.args.kwarg is None, "un `**kwargs` reabriría la puerta sin nombrarla"


@MODELADO
def test_la_puerta_publica_no_admite_identidad_por_parametro() -> None:
    """La misma propiedad sobre la función VIVA: un decorador podría reabrir lo que el AST no ve."""
    from vp_model import persist_forecasts as pf

    params = set(inspect.signature(pf.read_accredited).parameters)
    assert params == {"table", "reports"}, f"la frontera pública creció: {sorted(params)}"
    for prohibido in PROHIBIDOS:
        assert prohibido not in params, f"`{prohibido}` es una palanca para encoger lo que se comprueba"


@MODELADO
def test_el_camino_completo_y_coherente_SI_se_acredita(tmp_path: Path, monkeypatch) -> None:
    """El control. Sin él, «no pasa» podría ser el veredicto de todo y el gate sería un muro."""
    from tests import holdout_fixture as hf
    from vp_model import persist_forecasts as pf

    reports = hf.escena_coherente(tmp_path, monkeypatch)
    hf.artefacto_completo(reports, "FAD")
    assert len(pf.read_accredited("FAD", reports=reports)) == 5400


# ═══════════════════════════════ 2 · el recibo se compara ENTERO
@MODELADO
@pytest.mark.parametrize(
    "campo,valor,patron",
    [
        ("table", "DFF", "tabla"),
        ("pool", ["ets"], "pool"),
        ("n_expected_keys", 7, "esperar"),
    ],
)
def test_un_recibo_que_miente_en_cualquier_campo_no_pasa(tmp_path, monkeypatch, campo, valor, patron) -> None:
    """★ R2 sólo comparaba `n_rows` y `n_keys`. Se puede mentir en cualquier otro campo igual de fácil."""
    from tests import holdout_fixture as hf
    from vp_model import persist_forecasts as pf

    reports = hf.escena_coherente(tmp_path, monkeypatch)
    destino = hf.artefacto_completo(reports, "FAD")
    recibo = destino.with_name(destino.name + ".receipt.json")
    acta = json.loads(recibo.read_text(encoding="utf-8"))
    acta[campo] = valor
    recibo.write_text(json.dumps(acta), encoding="utf-8")
    with pytest.raises(pf.HoldoutForecastsError, match=patron):
        pf.read_accredited("FAD", reports=reports)


@MODELADO
def test_un_censo_que_miente_en_los_modelos_no_pasa(tmp_path, monkeypatch) -> None:
    """`models` y `n_models` también son afirmaciones, no adornos."""
    from tests import holdout_fixture as hf
    from vp_model import persist_forecasts as pf

    reports = hf.escena_coherente(tmp_path, monkeypatch)
    destino = hf.artefacto_completo(reports, "FAD")
    recibo = destino.with_name(destino.name + ".receipt.json")
    acta = json.loads(recibo.read_text(encoding="utf-8"))
    acta["coverage"]["models"] = ["ets"]
    recibo.write_text(json.dumps(acta), encoding="utf-8")
    with pytest.raises(pf.HoldoutForecastsError, match="models"):
        pf.read_accredited("FAD", reports=reports)


@MODELADO
@pytest.mark.parametrize("mutacion", ["sobra", "falta"])
def test_el_esquema_del_recibo_esta_cerrado_en_LOS_DOS_sentidos(tmp_path, monkeypatch, mutacion) -> None:
    """Un esquema abierto admite campos que nadie mira; uno incompleto, comprobaciones que no corren."""
    from tests import holdout_fixture as hf
    from vp_model import persist_forecasts as pf

    reports = hf.escena_coherente(tmp_path, monkeypatch)
    destino = hf.artefacto_completo(reports, "FAD")
    recibo = destino.with_name(destino.name + ".receipt.json")
    acta = json.loads(recibo.read_text(encoding="utf-8"))
    if mutacion == "sobra":
        acta["campo_que_nadie_mira"] = 1
    else:
        del acta["pool"]
    recibo.write_text(json.dumps(acta), encoding="utf-8")
    with pytest.raises(pf.HoldoutForecastsError, match="esquema abierto"):
        pf.read_accredited("FAD", reports=reports)


@MODELADO
def test_el_censo_de_cobertura_tiene_claves_exactas(tmp_path, monkeypatch) -> None:
    from tests import holdout_fixture as hf
    from vp_model import persist_forecasts as pf

    reports = hf.escena_coherente(tmp_path, monkeypatch)
    destino = hf.artefacto_completo(reports, "FAD")
    recibo = destino.with_name(destino.name + ".receipt.json")
    acta = json.loads(recibo.read_text(encoding="utf-8"))
    acta["coverage"].pop("n_models")
    recibo.write_text(json.dumps(acta), encoding="utf-8")
    with pytest.raises(pf.HoldoutForecastsError, match="censo de cobertura"):
        pf.read_accredited("FAD", reports=reports)


# ═══════════════════════════════ 3 · las invariantes de identidad siguen vivas por la puerta pública
@MODELADO
@pytest.mark.parametrize("estado", ["failed", "computed", "validated", "published"])
def test_por_la_puerta_publica_un_estado_terminal_tampoco_pasa(tmp_path, monkeypatch, estado) -> None:
    """R2 lo probaba sobre `campaign_identity`; ahora también sobre lo que usan los consumidores."""
    from tests import holdout_fixture as hf
    from vp_model import artifact_receipt as ar
    from vp_model import persist_forecasts as pf

    reports = hf.escena_coherente(tmp_path, monkeypatch, status=estado)
    hf.artefacto_completo(reports, "FAD")
    with pytest.raises(ar.ReceiptError, match="'running'"):
        pf.read_accredited("FAD", reports=reports)


@MODELADO
def test_por_la_puerta_publica_un_sha_que_no_es_el_HEAD_vivo_no_pasa(tmp_path, monkeypatch) -> None:
    from tests import holdout_fixture as hf
    from vp_model import artifact_receipt as ar
    from vp_model import persist_forecasts as pf

    reports = hf.escena_coherente(tmp_path, monkeypatch, sha="a" * 40)
    hf.artefacto_completo(reports, "FAD")
    with pytest.raises(ar.ReceiptError):
        pf.read_accredited("FAD", reports=reports)


@MODELADO
def test_por_la_puerta_publica_un_panel_distinto_no_pasa(tmp_path, monkeypatch) -> None:
    from tests import holdout_fixture as hf
    from vp_model import artifact_receipt as ar
    from vp_model import persist_forecasts as pf

    reports = hf.escena_coherente(tmp_path, monkeypatch, psha="sha256:" + "0" * 64)
    hf.artefacto_completo(reports, "FAD")
    with pytest.raises(ar.ReceiptError, match="otros datos"):
        pf.read_accredited("FAD", reports=reports)


# ═══════════════════════════════ 4 · ningún consumidor recupera el atajo
def test_ningun_consumidor_pasa_identidad_a_la_puerta() -> None:
    """Por AST. Si mañana alguien vuelve a necesitar el atajo, que sea una decisión visible."""
    import ast

    culpables: list[str] = []
    for py in [*(RAIZ / "experiments").glob("*.py"), *(RAIZ / "vp_model").glob("*.py")]:
        arbol = ast.parse(py.read_text(encoding="utf-8"))
        for n in ast.walk(arbol):
            if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "read_accredited":
                # `kw.arg` es None en `**kwargs`, que también sería una forma de colar identidad.
                extra = {kw.arg or "**kwargs" for kw in n.keywords} - {"reports"}
                if extra:
                    culpables.append(f"{py.name}: {sorted(extra)}")
    assert not culpables, f"vuelven a pasar identidad por parámetro: {culpables}"
