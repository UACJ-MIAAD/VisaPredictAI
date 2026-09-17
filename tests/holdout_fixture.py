"""Ayudas para las pruebas que tocan `holdout_forecasts_*`.

★ **M74-E-R3.** R2 retiró el ayudante que sellaba fixtures sintéticos y escribí que «un fixture
sintético ya no puede acreditarse». **Era falso**: uno con la rejilla COMPLETA sí podía, pasando
`campaign_id`/`code_sha`/`panel_sha256` a `read_accredited` — un atajo que existía porque mis
propias pruebas dependían de él. Cerrado el atajo, la afirmación correcta es otra:

> un fixture **sólo** se acredita si construye una campaña coherente con el repositorio vivo:
> transacción `running`, `source_git_sha` = HEAD real, `panel_sha256` = el panel en disco, entorno
> a juego, y las 5 400 claves que el panel exige, con su recibo completo.

Que sea trabajoso es el punto: es exactamente lo que hace la campaña, y por eso vale como prueba.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]


def head_vivo() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=RAIZ, capture_output=True, text=True, check=True
    ).stdout.strip()


def panel_sha() -> str:
    from vp_model import artifact_receipt as ar

    return "sha256:" + ar.sha256_file(RAIZ / ar.PANEL_REL)


def escena_coherente(
    tmp_path: Path,
    monkeypatch,
    *,
    campaign_id: str = "camp_prueba",
    status: str = "running",
    sha: str | None = None,
    psha: str | None = None,
) -> Path:
    """Un `reports/` con una transacción que CUADRA con el repositorio vivo. Devuelve ese directorio.

    Los parámetros permiten torcer UNA cosa a la vez —el estado, el sha, el panel— para que cada
    prueba ataque una sola invariante y el resto siga siendo el control.
    """
    reports = tmp_path / "reports"
    (reports / "eval").mkdir(parents=True, exist_ok=True)
    (reports / "campaign").mkdir(parents=True, exist_ok=True)
    (reports / "campaign" / "campaign.json").write_text(
        json.dumps(
            {
                "campaign_id": campaign_id,
                "status": status,
                "source_git_sha": sha or head_vivo(),
                "panel_sha256": psha or panel_sha(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CAMPAIGN_ID", campaign_id)
    monkeypatch.setenv("CAMPAIGN_SHA", sha or head_vivo())
    return reports


def artefacto_completo(reports: Path, table: str, *, campaign_id: str = "camp_prueba") -> Path:
    """Escribe el artefacto con las claves REALES del panel y su recibo completo, y lo sella.

    No inventa la rejilla: la pide a `expected_keys`, que es la misma autoridad que el lector usa
    para volver a auditar. Un fixture que se derivara de otra parte estaría probando otra cosa.
    """
    from vp_model import artifact_receipt as ar
    from vp_model import persist_forecasts as pf

    esperado = pf.expected_keys(table)
    filas = sorted(esperado)
    destino = reports / "eval" / f"holdout_forecasts_{table}.csv"
    destino.write_text(
        "model,country,category,date,actual,forecast\n"
        + "\n".join(f"{m},{c},{k},{d},1.0,2.0" for (m, c, k, d) in filas)
        + "\n",
        encoding="utf-8",
    )
    modelos = sorted({m for m, _c, _k, _d in filas})
    ar.seal(
        destino,
        schema=pf.SCHEMA,
        campaign_id=campaign_id,
        code_sha=head_vivo(),
        panel_sha256=panel_sha(),
        protocol={**pf.PROTOCOL, "block": "family"},
        coverage={"n_rows": len(filas), "n_keys": len(filas), "models": modelos, "n_models": len(modelos)},
        expected_keys=esperado,
        extra={"pool": list(pf.HOLDOUT_POOL_MODELS), "table": table, "excluded": []},
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
    except (ar.ReceiptError, pf.HoldoutForecastsError) as exc:
        pytest.skip(f"sin holdout_forecasts_{table} acreditado en el repo: {exc}")
