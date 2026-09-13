"""Acredita un `holdout_forecasts_*` sintético para que las pruebas ejerciten el camino REAL.

Desde M74-E-R1 los consumidores re-acreditan el artefacto en cada lectura. Una prueba que escriba
un CSV suelto y espere que se lea estaría comprobando un camino que ya no existe; y relajar el gate
para que pasara sería deshacer justamente lo que el lote arregla. Así que la prueba sella su propio
fixture: una campaña sintética, un recibo válido, y el mismo régimen que exige el lector.

⚠️ El import del producto va DENTRO de la función: este archivo vive en `tests/` y el job base sólo
instala `.[dev]`. Es la trampa que este repositorio ha pisado ocho veces.
"""

from __future__ import annotations

import json
from pathlib import Path

CAMPAIGN_ID = "prueba_sintetica_0001"
CODE_SHA = "0" * 40
PANEL_SHA = "sha256:" + "1" * 64


def acredita(reports: Path, table: str, monkeypatch, *, block: str = "family") -> Path:
    """Sella el CSV que ya existe en ``reports/eval`` y deja la identidad en el entorno."""
    from vp_model import artifact_receipt as ar
    from vp_model import persist_forecasts as pf

    (reports / "campaign").mkdir(parents=True, exist_ok=True)
    (reports / "campaign" / "campaign.json").write_text(
        json.dumps({"campaign_id": CAMPAIGN_ID, "panel_sha256": PANEL_SHA}), encoding="utf-8"
    )
    monkeypatch.setenv("CAMPAIGN_ID", CAMPAIGN_ID)
    monkeypatch.setenv("CAMPAIGN_SHA", CODE_SHA)
    monkeypatch.setattr(pf, "REPORTS", reports)
    destino = reports / "eval" / f"holdout_forecasts_{table}.csv"
    ar.seal(
        destino,
        schema=pf.SCHEMA,
        campaign_id=CAMPAIGN_ID,
        code_sha=CODE_SHA,
        panel_sha256=PANEL_SHA,
        protocol={**pf.PROTOCOL, "block": block},
        coverage={"fixture": True},
    )
    return destino


def salta_si_no_hay_artefacto_acreditado(table: str) -> None:
    """Para las pruebas de integración que leen el artefacto VIVO del repositorio.

    ★ Antes pasaban leyendo `holdout_forecasts_{table}.csv` **sin recibo y de otra añada** —medido:
    472 claves ausentes y 448 no esperadas en FAD contra el panel de hoy—. Acreditar ese archivo
    para que siguieran verdes sería sellar justamente lo que el lote declara no fiable, así que se
    saltan **diciendo por qué** hasta que una campaña deje uno acreditado.
    """
    import pytest

    from vp_model import artifact_receipt as ar
    from vp_model import persist_forecasts as pf

    try:
        pf.read_accredited(table)
    except ar.ReceiptError as exc:
        pytest.skip(f"sin holdout_forecasts_{table} acreditado en el repo: {exc}")
