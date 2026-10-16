"""M74-F · G7 · B3-bis — el pin de `cohort_scan.json` y la salida de la campaña: dos identidades, una evidencia.

RED (auditoría de B3, 16-oct, sobre `4950bd5`): el deck fija como entrada de E3 el escaneo de cohortes
PRE-REGISTRADO (`806b3f…`), pero la campaña causal re-derivó el escaneo dentro de su transacción y el árbol trae
esa salida (`9dc3cf…`): `test_the_e3_deck_inputs_match_the_tree_after_the_reseal` caía. Reescribir el pin
borraría la identidad del pre-registro; aceptar la divergencia sin más no diría qué representa cada archivo.

La evidencia versionada (`docs/evidence/g7/`) lleva el pre-registro byte a byte y un linaje que declara ambas
identidades, lo que NO cambia (la composición pre-registrada) y lo que SÍ (campos de modelo y veredictos). Estas
pruebas lo recalculan todo desde los dos archivos y exigen que la declaración COINCIDA: funcionan en un clon limpio.
El contraste con los originales sellados (objeto git del pre-registro y miembro del tar de la madre) es local.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import tarfile
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
LINAJE = ROOT / "docs" / "evidence" / "g7" / "cohort_scan_linaje.json"
#: Un campo que sólo existe en uno de los dos escaneos también es un cambio (la adversarial `_otro_campo` lo cazó).
_AUSENTE = object()


def _sha(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


def _json(rel: str) -> Any:
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def _norm(v: Any) -> Any:
    """Una lista como conjunto ordenado de sus elementos (también si son diccionarios); el resto, tal cual."""
    return sorted(json.dumps(x, sort_keys=True) for x in v) if isinstance(v, list) else v


def _cambios(pre: dict, post: dict) -> tuple[dict[str, int], list[dict]]:
    """Campos de modelo que cambian (cuántas veces) y veredictos que cambian, celda a celda."""
    campos: Counter[str] = Counter()
    veredictos = []
    for x, y in zip(pre["cells"], post["cells"], strict=True):
        despues = {m["model"]: m for m in y["models"]}
        for m in x["models"]:
            d = despues[m["model"]]
            campos.update(f for f in sorted(set(m) | set(d)) if m.get(f, _AUSENTE) != d.get(f, _AUSENTE))
            if m["verdict_exploratory"] != despues[m["model"]]["verdict_exploratory"]:
                veredictos.append({"table": x["table"], "cohort": x["cohort"], "model": m["model"],
                                   "from": m["verdict_exploratory"], "to": despues[m["model"]]["verdict_exploratory"]})  # fmt: skip
    return dict(sorted(campos.items())), veredictos


def problemas(linaje: dict, deck: dict, pre_bytes: bytes, post_bytes: bytes) -> list[str]:
    """Todo lo que el linaje afirma, recalculado desde los dos archivos. Vacío = acreditado."""
    p: list[str] = []
    pre, post = json.loads(pre_bytes), json.loads(post_bytes)
    clave = linaje["deck"]["key"]
    for pin in linaje["deck"]["pins"]:
        if deck[pin][clave] != linaje["preregistration"]["sha256"]:
            p.append(f"el pin {pin}.{clave} del deck no es el pre-registro que declara el linaje")
    if _sha(pre_bytes) != linaje["preregistration"]["sha256"]:
        p.append("la copia versionada no es el pre-registro declarado")
    if _sha(post_bytes) != linaje["campaign_output"]["sha256"]:
        p.append("el escaneo del árbol no es la salida de la campaña declarada")
    for k in linaje["unchanged"]["top_level"]:
        if pre[k] != post[k]:
            p.append(f"{k} cambió y el linaje dice que no")
    if len(pre["cells"]) != len(post["cells"]):
        return [*p, "cambió el número de celdas"]
    for x, y in zip(pre["cells"], post["cells"], strict=True):
        celda = f"{x['table']}/{x['cohort']}"
        for k in linaje["unchanged"]["cell"]:
            if _norm(x[k]) != _norm(y[k]):
                p.append(f"{celda}: {k} cambió y el linaje dice que no")
        if sorted(m["model"] for m in x["models"]) != sorted(m["model"] for m in y["models"]):
            p.append(f"{celda}: cambió el conjunto de modelos")
        if (x["mcs90"] is None) != (y["mcs90"] is None):
            p.append(f"{celda}: el MCS apareció o desapareció")
        elif x["mcs90"] is not None:
            for k in ("members", "eliminated", *linaje["unchanged"]["cell_mcs90"]):
                if _norm(x["mcs90"][k]) != _norm(y["mcs90"].get(k)):
                    p.append(f"{celda}: mcs90.{k} cambió y el linaje dice que no")
    if p:
        return p
    campos, veredictos = _cambios(pre, post)
    if campos != linaje["changed_model_fields"]:
        p.append(f"los campos que cambian no son los declarados: {campos}")
    if veredictos != linaje["verdict_changes"]:
        p.append(f"los veredictos que cambian no son los declarados: {veredictos}")
    return p


@pytest.fixture(scope="module")
def real() -> dict:
    linaje = json.loads(LINAJE.read_text(encoding="utf-8"))
    return {
        "linaje": linaje,
        "deck": _json(linaje["deck"]["path"]),
        "pre": (ROOT / linaje["preregistration"]["path"]).read_bytes(),
        "post": (ROOT / linaje["campaign_output"]["path"]).read_bytes(),
    }


def _p(m: dict, **cambios: Any) -> list[str]:
    a = {**m, **cambios}
    return problemas(a["linaje"], a["deck"], a["pre"], a["post"])


def test_el_linaje_real_se_acredita(real: dict) -> None:
    assert _p(real) == []


def test_reconoce_que_cambian_veredictos_no_solo_puntuaciones(real: dict) -> None:
    """La auditoría lo pidió explícito: en DFF estable, ARIMA y ETS dejan de ser peores que naïve-1."""
    assert {(v["model"], v["from"], v["to"]) for v in real["linaje"]["verdict_changes"]} == {
        ("arima", "peor_que_naive1", "sin_evidencia"),
        ("ets", "peor_que_naive1", "sin_evidencia"),
    }
    assert {"p_value", "ci_low", "n_pairs"} <= set(real["linaje"]["changed_model_fields"])


def test_la_salida_deriva_de_los_pools_y_las_cohortes_del_arbol(real: dict) -> None:
    pv = json.loads(real["post"])["provenance"]
    assert _sha((ROOT / pv["cohorts"]).read_bytes()) == pv["cohorts_sha256"]
    for nombre, sha in pv["scorecards"].items():
        assert _sha((ROOT / "reports" / "eval" / nombre).read_bytes()) == sha, nombre


# ═══════════════════════════════ adversariales: la acreditación tiene que poder fallar
def _mutar(m: dict, cambio) -> bytes:
    post = json.loads(m["post"])
    cambio(post)
    return json.dumps(post).encode()


def _mover_serie(d: dict) -> None:
    d["cells"][1]["series"].append(d["cells"][0]["series"].pop())


def _otro_veredicto(d: dict) -> None:
    d["cells"][0]["models"][0]["verdict_exploratory"] = "bate_naive1"


def _otro_mcs(d: dict) -> None:
    d["cells"][0]["mcs90"]["members"] = ["ets"]


def _otro_campo(d: dict) -> None:
    d["cells"][0]["models"][0]["rank_global"] = 1


@pytest.mark.parametrize("cambio", [_mover_serie, _otro_veredicto, _otro_mcs, _otro_campo])
def test_un_escaneo_que_no_es_el_declarado_no_se_acredita(real: dict, cambio) -> None:
    falso = _mutar(real, cambio)
    linaje = copy.deepcopy(real["linaje"])
    linaje["campaign_output"]["sha256"] = _sha(falso)  # aunque el linaje se re-selle con el hash nuevo
    assert _p(real, post=falso, linaje=linaje)


def test_un_pin_reescrito_no_se_acredita(real: dict) -> None:
    deck = copy.deepcopy(real["deck"])
    deck["inputs"]["cohort_scan.json"] = real["linaje"]["campaign_output"]["sha256"]
    assert any("pin inputs.cohort_scan.json" in x for x in _p(real, deck=deck))


def test_un_preregistro_editado_no_se_acredita(real: dict) -> None:
    assert any("copia versionada" in x for x in _p(real, pre=real["pre"] + b" "))


# ═══════════════════════════════ contraste LOCAL con los originales sellados
def test_local_la_copia_es_el_objeto_git_del_preregistro(real: dict) -> None:
    blob = real["linaje"]["preregistration"]["git_blob"]
    r = subprocess.run(["git", "-C", str(ROOT), "cat-file", "blob", blob], capture_output=True, check=False)
    if r.returncode != 0:
        pytest.skip("el objeto git del pre-registro no está en este clon (clon superficial)")
    assert r.stdout == real["pre"]


def test_local_el_arbol_trae_el_miembro_sellado_de_la_madre(real: dict) -> None:
    tar = os.environ.get("VP_G7_TAR_MADRE")
    if not tar:
        pytest.skip("VP_G7_TAR_MADRE no apunta al tar sellado de la madre (contraste sólo local)")
    with tarfile.open(tar, "r:*") as t:
        miembro = t.extractfile(real["linaje"]["campaign_output"]["archive_member"])
        assert miembro is not None
        assert _sha(miembro.read()) == real["linaje"]["campaign_output"]["sha256"] == _sha(real["post"])
