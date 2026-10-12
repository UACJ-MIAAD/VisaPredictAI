"""M74-F · deuda 1 — el CRPS campeón sólo es vigente si su recibo lo acredita AL LEER.

La cuarta campaña real (`rederiv_953cd00_20261006T073927`) dejó `crps_champion.csv` byte-idéntico al
de julio y su cifra (32.1/31.3) volvió a llegar a ``key_facts`` y a ``\\factCrpsFad`` como si la
hubiera calculado la campaña. Estas pruebas fijan la conducta del lector y del emisor de hechos:
todo en directorios temporales, sin tocar el ``reports/`` vivo.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytest.importorskip("scipy")
pytest.importorskip("darts")

from vp_model import artifact_receipt as ar  # noqa: E402
from vp_model import champion  # noqa: E402

CSV = "table,country,category,crps\nFAD,ALL,ALL,32.1\nDFF,ALL,ALL,31.3\n"


def _bkf():
    spec = importlib.util.spec_from_file_location("build_key_facts_m74f", ROOT / "experiments" / "build_key_facts.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mundo(tmp_path: Path):
    reports = tmp_path / "reports"
    (reports / "eval").mkdir(parents=True)
    panel = tmp_path / "panel.csv"
    panel.write_text("country,category,value\nmexico,F1,1\n")
    return reports, panel


def _sellar(reports: Path, panel: Path, campaign_id: str = "rederiv_x_1", panel_sha: str | None = None) -> Path:
    csv = reports / "eval" / "crps_champion.csv"
    csv.write_text(CSV)
    return ar.seal(
        csv,
        schema=champion.CRPS_RECEIPT_SCHEMA,
        campaign_id=campaign_id,
        code_sha="a" * 40,
        panel_sha256=panel_sha or ("sha256:" + ar.sha256_file(panel)),
        protocol={"tables": ["FAD", "DFF"]},
        coverage={"tables": ["DFF", "FAD"]},
    )


def _transaccion(reports: Path, panel: Path, campaign_id: str) -> None:
    (reports / "campaign").mkdir(parents=True, exist_ok=True)
    (reports / "campaign" / "campaign.json").write_text(
        json.dumps(
            {
                "status": "running",
                "campaign_id": campaign_id,
                "source_git_sha": "a" * 40,
                "panel_sha256": "sha256:" + ar.sha256_file(panel),
            }
        )
    )


def test_sin_csv_es_ausente(mundo) -> None:
    reports, panel = mundo
    p = champion.crps_provenance("FAD", reports=reports, panel=panel)
    assert p["status"] == "ausente" and p["value"] is None and p["inherited_value"] is None


def test_csv_sin_recibo_es_heredado_y_conserva_su_valor_identificado(mundo) -> None:
    """El caso exacto de la cuarta campaña: el CSV está, nadie dice de cuándo es."""
    reports, panel = mundo
    (reports / "eval" / "crps_champion.csv").write_text(CSV)
    p = champion.crps_provenance("FAD", reports=reports, panel=panel)
    assert p["status"] == "heredado"
    assert p["value"] is None, "un CRPS sin recibo NO puede salir como vigente"
    assert p["inherited_value"] == pytest.approx(32.1)
    assert p["artifact_sha256"] == ar.sha256_file(reports / "eval" / "crps_champion.csv")


def test_recibo_valido_para_el_panel_de_hoy_acredita_con_su_transaccion(mundo) -> None:
    """#66: esta prueba acreditaba un recibo SIN transacción alguna; era el agujero, no una garantía.
    Ahora la campaña que lo produjo tiene que existir en el árbol (`test_m74f_crps_lector_transaccion`)."""
    reports, panel = mundo
    _sellar(reports, panel)
    assert champion.crps_provenance("DFF", reports=reports, panel=panel)["status"] == "heredado"
    _transaccion(reports, panel, "rederiv_x_1")
    p = champion.crps_provenance("DFF", reports=reports, panel=panel)
    assert p["status"] == "acreditado" and p["value"] == pytest.approx(31.3)
    assert p["inherited_value"] is None


def test_recibo_de_otro_panel_no_acredita(mundo) -> None:
    reports, panel = mundo
    _sellar(reports, panel, panel_sha="sha256:" + "0" * 64)
    p = champion.crps_provenance("FAD", reports=reports, panel=panel)
    assert p["status"] == "heredado" and "otro panel" in p["reason"]


def test_csv_tocado_despues_del_sellado_no_acredita(mundo) -> None:
    reports, panel = mundo
    _sellar(reports, panel)
    (reports / "eval" / "crps_champion.csv").write_text(CSV.replace("32.1", "12.0"))
    p = champion.crps_provenance("FAD", reports=reports, panel=panel)
    assert p["status"] == "heredado" and p["inherited_value"] == pytest.approx(12.0)


def test_esquema_ajeno_no_acredita(mundo) -> None:
    reports, panel = mundo
    recibo = _sellar(reports, panel)
    acta = json.loads(recibo.read_text())
    acta["schema"] = "holdout-forecasts-receipt/1"
    recibo.write_text(json.dumps(acta))
    assert champion.crps_provenance("FAD", reports=reports, panel=panel)["status"] == "heredado"


def test_dentro_de_campana_el_recibo_tiene_que_ser_de_esa_campana(mundo) -> None:
    reports, panel = mundo
    _sellar(reports, panel, campaign_id="rederiv_vieja")
    _transaccion(reports, panel, "rederiv_nueva")
    p = champion.crps_provenance("FAD", reports=reports, panel=panel)
    assert p["status"] == "heredado" and "rederiv_vieja" in p["reason"]
    # control benigno: el MISMO recibo bajo SU campaña sí acredita (el gate no es un muro)
    _transaccion(reports, panel, "rederiv_vieja")
    assert champion.crps_provenance("FAD", reports=reports, panel=panel)["status"] == "acreditado"


def test_key_facts_no_publica_como_vigente_un_crps_heredado() -> None:
    bkf = _bkf()
    estados: set[str] = set()
    d = {
        "champion_crps": None,
        "champion_crps_provenance": {
            "status": "heredado",
            "value": None,
            "inherited_value": 32.1,
            "artifact_sha256": "efd2cbd51e561e8d5a0c8f8a16a481ac68027b7c4769eabb2850ebcfcec8fd34",
        },
    }
    out = bkf._crps_facts("FAD", d, estados)
    assert "crps_fad" not in out, "la clave vigente desaparece: nadie puede citarla sin decidirlo"
    assert out == {"crps_fad_heredado": 32.1, "crps_heredado_sha": "efd2cbd51e56"}
    assert estados == {"heredado"}


def test_key_facts_publica_el_crps_acreditado_con_su_clave_de_siempre() -> None:
    bkf = _bkf()
    estados: set[str] = set()
    d = {"champion_crps": 30.04, "champion_crps_provenance": {"status": "acreditado", "value": 30.04}}
    assert bkf._crps_facts("DFF", d, estados) == {"crps_dff": 30.0}
    assert estados == {"acreditado"}


def test_un_veredicto_antiguo_sin_procedencia_es_heredado() -> None:
    """`champion_challenger.json` previo a M74-F trae el número sin decir de dónde salió."""
    bkf = _bkf()
    estados: set[str] = set()
    assert bkf._crps_facts("FAD", {"champion_crps": 32.1}, estados) == {"crps_fad_heredado": 32.1}
    assert estados == {"heredado"}
