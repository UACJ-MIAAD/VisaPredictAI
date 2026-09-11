"""M74-B-R1 · La revisión humana se ACREDITA, y no hay puerta trasera para saltársela.

Dos bloqueos reales que la revisión del autor encontró sobre M74-B:

1. ``--skip-consistency-check`` era un **bypass de producción**: convertía `computed` +
   `consistency: pending` en `validated` sin ejecutar el guardián. Y la propia suite lo usaba, que
   es como los bypasses sobreviven a las revisiones — alguien los necesita para que un test pase.
2. La «revisión humana» no se acreditaba: cualquier proceso podía pasar ``--reviewed-by cron-bot``,
   ``--decision approved`` y **cualquier archivo** como recibo. La máquina no tenía con qué
   contradecirle.

Aquí se prueba lo contrario, ejecutando: la bandera **no existe**, y el recibo es un artefacto de
esquema cerrado ligado a ESTA campaña por tres identidades, con revisor humano y fecha posterior a
lo que revisa. Nada de esto corre una campaña: lanes sintéticos en temporales.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools import campaign_state as cs
from tools import campaign_txn as txn

RAIZ = Path(__file__).resolve().parents[1]
SHA = "a" * 40


@pytest.fixture
def campana(tmp_path: Path) -> Path:
    """Una campaña sintética en `computed`, lista para que alguien intente validarla."""
    (tmp_path / "reports" / "campaign").mkdir(parents=True)
    panel = tmp_path / "panel.csv"
    panel.write_text("country,category,table,value\nmexico,EB2,FAD,1\n", encoding="utf-8")
    ruta = tmp_path / "reports" / "campaign" / "campaign.json"
    txn.open_campaign(ruta, campaign_id="r1_sintetica", source_git_sha=SHA, git_dirty=False, panel=panel)
    cs.mark_computed(
        ruta,
        completed_at=txn.now_rfc3339(),
        input_gate="passed",
        output_gate="passed",
        consistency=cs._GATE_PENDING,  # ← el caso incómodo: cifras cambiadas, sin propagar
    )
    return ruta


def _acta(ruta: Path, **cambios) -> Path:
    estado = cs.read(ruta) or {}
    datos = {
        "schema": txn.RECEIPT_SCHEMA,
        "campaign_id": estado["campaign_id"],
        "source_git_sha": estado["source_git_sha"],
        "panel_sha256": estado["panel_sha256"],
        "reviewed_by": "Javier Rebull",
        "decision": "aprobada",
        "reviewed_at": txn.now_rfc3339(),
    }
    datos.update(cambios)
    for k in [k for k, v in datos.items() if v is None]:  # None = «quita esta clave»
        del datos[k]
    destino = ruta.parent / f"acta_{len(list(ruta.parent.glob('acta_*.json')))}.json"
    destino.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
    return destino


# ═════════════════════════════════════════════ 1 · el bypass no existe
def test_the_consistency_bypass_flag_is_gone_from_the_cli() -> None:
    """RED permanente: si alguien reintroduce la bandera, argparse deja de rechazarla."""
    fin = subprocess.run(
        [sys.executable, "-m", "tools.campaign_txn", "--path", "x.json", "validate",
         "--receipt", "r.json", "--skip-consistency-check"],
        cwd=RAIZ, capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    assert fin.returncode == 2, "la CLI debe RECHAZAR la bandera, no ignorarla"
    assert "unrecognized arguments" in fin.stderr


def test_validate_takes_no_free_reviewer_or_decision() -> None:
    """El revisor y la decisión ya no son argumentos: se leen del recibo."""
    for libre in ("--reviewed-by", "--decision"):
        fin = subprocess.run(
            [sys.executable, "-m", "tools.campaign_txn", "--path", "x.json", "validate",
             "--receipt", "r.json", libre, "lo-que-sea"],
            cwd=RAIZ, capture_output=True, text=True, timeout=120,
        )  # fmt: skip
        assert fin.returncode == 2, f"{libre} no puede seguir aceptándose"
    # y no hay ninguna otra bandera por la que colarse: lo que `validate` acepta es exactamente
    # `--receipt`, más el `--path` global y el nombre del subcomando
    recibido = txn._parser().parse_args(["--path", "x.json", "validate", "--receipt", "r.json"])
    assert set(vars(recibido)) == {"cmd", "path", "receipt"}, f"validate expone argumentos de más: {vars(recibido)}"


def test_a_failing_guardian_blocks_validation_and_leaves_the_state_untouched(
    campana: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La consistencia pendiente NO valida: manda el veredicto del guardián, no la buena fe."""
    monkeypatch.setattr(txn, "_run_consistency", lambda: (False, "cifras cambiadas sin propagar"))
    recibo = _acta(campana)
    assert txn.main(["--path", str(campana), "validate", "--receipt", str(recibo)]) == txn.EXIT_ERROR
    quedo = cs.read(campana)
    assert quedo is not None
    assert quedo["status"] == "computed", "un guardián en rojo no puede dejar la campaña validada"
    assert quedo["consistency"] == cs._GATE_PENDING, "ni puede blanquear la consistencia pendiente"
    assert not txn.publishable(campana)[0]


def test_the_guardian_fails_closed_when_it_cannot_be_found(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Si el guardián no está donde debe, la respuesta es NO, no «no pude comprobarlo»."""
    monkeypatch.setattr(txn, "_REPO_ROOT", tmp_path)
    ok, motivo = txn._run_consistency()
    assert ok is False and "check_consistency.py" in motivo


# ═════════════════════════════════════════════ 2 · el recibo acredita, o no vale
def test_an_arbitrary_file_is_not_a_validation_receipt(campana: Path) -> None:
    """★ El bloqueo exacto que señaló la revisión: cualquier archivo servía de recibo."""
    for nombre, contenido in (
        ("recibo.md", "revisión humana de la campaña\n"),
        ("vacio.json", ""),
        ("lista.json", "[1, 2, 3]"),
        ("suelto.json", '{"aprobado": true}'),
    ):
        suelto = campana.parent / nombre
        suelto.write_text(contenido, encoding="utf-8")
        with pytest.raises(txn.ReceiptError):
            txn.load_validation_receipt(suelto, cs.read(campana) or {})
    with pytest.raises(txn.ReceiptError, match="no existe"):
        txn.load_validation_receipt(campana.parent / "fantasma.json", cs.read(campana) or {})


@pytest.mark.parametrize(
    "cambio,patron",
    [
        ({"reviewed_by": "cron-bot"}, "automatizada"),
        ({"reviewed_by": "ci"}, "corto"),
        ({"reviewed_by": "github-actions"}, "automatizada"),
        ({"reviewed_by": "release runner"}, "automatizada"),
        ({"reviewed_by": "service-account"}, "automatizada"),
        ({"reviewed_by": "   "}, "cadena no vacía"),
        ({"reviewed_by": "JR"}, "corto"),
    ],
)
def test_an_empty_or_automated_reviewer_does_not_validate(campana: Path, cambio: dict, patron: str) -> None:
    """★ El segundo bloqueo: `--reviewed-by cron-bot` acreditaba una «revisión humana»."""
    with pytest.raises(txn.ReceiptError, match=patron):
        txn.load_validation_receipt(_acta(campana, **cambio), cs.read(campana) or {})


@pytest.mark.parametrize(
    "cambio,patron",
    [
        ({"campaign_id": "otra_campana"}, "misma corrida"),
        ({"source_git_sha": "b" * 40}, "misma corrida"),
        ({"panel_sha256": "c" * 64}, "misma corrida"),
        ({"schema": "campaign-validation-receipt/99"}, "se esperaba"),
        ({"decision": "approved"}, "vocabulario"),
        ({"decision": "rechazada"}, "RECHAZA"),
        ({"reviewed_at": "ayer por la tarde"}, "RFC3339"),
        ({"reviewed_at": "2020-01-01T00:00:00Z"}, "precede"),
        ({"schema": None}, "faltan"),
        ({"reviewed_by": None}, "faltan"),
        ({"extra": "de más"}, "sobran"),
    ],
)
def test_the_receipt_is_bound_to_this_campaign_and_nothing_else(campana: Path, cambio: dict, patron: str) -> None:
    with pytest.raises(txn.ReceiptError, match=patron):
        txn.load_validation_receipt(_acta(campana, **cambio), cs.read(campana) or {})


def test_a_receipt_with_duplicate_keys_is_refused(campana: Path) -> None:
    """Un segundo `decision` escondido no puede colarse por el último valor que gane."""
    estado = cs.read(campana) or {}
    crudo = (
        f'{{"schema": "{txn.RECEIPT_SCHEMA}", "campaign_id": "{estado["campaign_id"]}", '
        f'"source_git_sha": "{estado["source_git_sha"]}", "panel_sha256": "{estado["panel_sha256"]}", '
        f'"reviewed_by": "Javier Rebull", "decision": "rechazada", "decision": "aprobada", '
        f'"reviewed_at": "{txn.now_rfc3339()}"}}'
    )
    suelto = campana.parent / "duplicada.json"
    suelto.write_text(crudo, encoding="utf-8")
    with pytest.raises(txn.ReceiptError, match="duplicadas"):
        txn.load_validation_receipt(suelto, estado)


def test_non_string_values_are_refused(campana: Path) -> None:
    """`decision: true` no es una decisión."""
    estado = cs.read(campana) or {}
    suelto = campana.parent / "tipos.json"
    suelto.write_text(
        json.dumps(
            {
                "schema": txn.RECEIPT_SCHEMA,
                "campaign_id": estado["campaign_id"],
                "source_git_sha": estado["source_git_sha"],
                "panel_sha256": estado["panel_sha256"],
                "reviewed_by": "Javier Rebull",
                "decision": True,
                "reviewed_at": txn.now_rfc3339(),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(txn.ReceiptError, match="cadena no vacía"):
        txn.load_validation_receipt(suelto, estado)


# ═════════════════════════════════════════════ 3 · el camino que SÍ valida
def test_a_real_receipt_validates_and_the_reviewer_is_derived_not_typed(
    campana: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Con el guardián en verde y un recibo acreditado, la campaña llega a `validated`.

    Y lo que queda escrito en el estado **sale del recibo**: nadie lo tecleó en la línea de órdenes.
    """
    monkeypatch.setattr(txn, "_run_consistency", lambda: (True, ""))
    recibo = _acta(campana, reviewed_by="Javier Augusto Rebull Saucedo", decision="aprobada")
    assert txn.main(["--path", str(campana), "validate", "--receipt", str(recibo)]) == txn.EXIT_OK
    obj = cs.read(campana)
    assert obj is not None
    assert obj["status"] == "validated"
    assert obj["reviewed_by"] == "Javier Augusto Rebull Saucedo"
    assert obj["decision"] == "aprobada"
    assert obj["consistency"] == cs._GATE_OK, "validar acredita la consistencia que el cómputo dejó pendiente"
    # el recibo queda ligado por ruta ABSOLUTA y por hash: moverlo o editarlo rompe el permiso
    assert Path(obj["validation_receipt_path"]).is_absolute()
    assert obj["validation_receipt_sha256"] == txn.sha256_file(recibo)
    assert txn.publishable(campana)[0]
    recibo.write_text(recibo.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert not txn.publishable(campana)[0], "editar el recibo después retira el permiso"


def test_a_relative_receipt_path_cannot_be_verified(campana: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Un estado escrito a mano con ruta relativa no se resuelve a ciegas: es fail-closed.

    ⚠️ Antes se resolvía subiendo tres niveles desde `campaign.json`, así que mover el estado
    cambiaba qué archivo se verificaba.
    """
    monkeypatch.setattr(txn, "_run_consistency", lambda: (True, ""))
    recibo = _acta(campana)
    assert txn.main(["--path", str(campana), "validate", "--receipt", str(recibo)]) == txn.EXIT_OK
    obj = cs.read(campana) or {}
    crudo = json.loads(campana.read_text(encoding="utf-8"))
    crudo["validation_receipt_path"] = "reports/campaign/acta_0.json"
    campana.write_text(json.dumps(crudo), encoding="utf-8")
    ok, motivo = txn.publishable(campana)
    assert not ok and "relativa" in motivo
    assert obj["status"] == "validated"  # el estado seguía siendo válido; lo que no se acredita es el recibo
