"""Produce el artefacto de pronósticos TRANSPORTADOS. El productor que faltaba (§8.7).

M74-E construyó la tubería entera del transporte —constructor de filas, artefacto, recibo por
conjunto exacto de claves, cuatro REDs adversariales, consumidor fail-closed— y **dejó sin cablear
el productor**. `transported_forecasts.write()` no lo llamaba nadie: sus dos únicas referencias
eran consumidores. El efecto era exactamente el defecto que este lote vino a cerrar —código que se
anuncia y no se ejecuta— sólo que esta vez el mío: el exportador habría fallado cerrado buscando un
artefacto que ninguna etapa escribe, y `ets`/`theta` habrían seguido ausentes.

Peor: mis REDs pasaban igual, porque probaban `write()` y `load_and_accredit()` con datos
sintéticos. Ninguno comprobaba que **alguien invocara** `write()` en el camino real. Es la lección
de M41-R1 —probar el comportamiento, no las piezas— que no apliqué a mi propio módulo.

Corre en ``ante``. Uso:  ante/bin/python experiments/persist_transported_forecasts.py
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "experiments"))


def universo_vigente() -> list[tuple[str, str, str]]:
    """Las combinaciones elegibles ``(table, country, category)``, del catálogo vivo."""
    from vp_model import config, dataset

    combos: list[tuple[str, str, str]] = []
    for table in config.TABLES:
        cat = dataset.list_series(table=table, block="family", countries=config.PILOT_COUNTRIES)
        combos += [(table, r.country, r.category) for r in cat.itertuples()]
    return combos


def objetivos_de(table: str, country: str, category: str) -> list[tuple[str, str]]:
    """Los ``(origin, target)`` del hold-out. ``origin`` por **periodo mensual**, nunca por días."""
    from vp_model import dataset, models, walkforward

    ts = models.to_timeseries(dataset.load_series(country, category, table))
    return [
        ((pd.Timestamp(d).to_period("M") - 1).to_timestamp().strftime("%Y-%m-%d"), pd.Timestamp(d).strftime("%Y-%m-%d"))
        for d in ts.time_index[-walkforward.HOLDOUT :]
    ]


def recoger_filas(
    universo: list[tuple[str, str, str]],
    modelos: tuple[str, ...],
    *,
    backtest: Callable[..., Any] | None = None,
) -> list[dict[str, Any]]:
    """Recoge las filas largas que el walk-forward YA calcula, sin recalcular nada.

    ``backtest`` entra por parámetro para que una prueba de integración pueda ejercitar el camino
    real —productor, artefacto, recibo y consumidor— sin entrenar 25 series por dos modelos. La
    costura es del test; en producción se usa el walk-forward oficial.
    """
    if backtest is None:
        from vp_model import walkforward

        backtest = walkforward.backtest

    filas: list[dict[str, Any]] = []
    for table, country, category in universo:
        for modelo in modelos:
            resultado = backtest(modelo, country, category, table)
            propias = getattr(resultado, "holdout_rows", None)
            if not propias:
                raise SystemExit(
                    f"✗ {table}/{country}/{category}/{modelo}: el walk-forward no devolvió filas de "
                    "hold-out. Sin ellas el transporte no existe y ETS/Theta quedarían fuera del "
                    "export; no se rellena ni se supone."
                )
            filas += propias
    return filas


def main(argv: list[str] | None = None) -> int:
    from vp_model import transported_forecasts as tf
    from vp_model.model_registry import TRANSPORTED_FORECAST_MODELS

    campaign_id = os.environ.get("CAMPAIGN_ID", "")
    code_sha = os.environ.get("CAMPAIGN_SHA", "")
    if not campaign_id or not code_sha:
        print(
            "✗ sin CAMPAIGN_ID/CAMPAIGN_SHA: el transporte se liga a una campaña concreta y correr "
            "suelto produciría un artefacto sin procedencia.",
            file=sys.stderr,
        )
        return 1

    universo = universo_vigente()
    objetivos = {c: objetivos_de(*c) for c in universo}
    esperado = tf.expected_keys(
        campaign_id=campaign_id, universe=universo, models=TRANSPORTED_FORECAST_MODELS, targets=objetivos
    )
    print(f"  universo: {len(universo)} series × {len(TRANSPORTED_FORECAST_MODELS)} modelos → {len(esperado)} claves")

    filas = recoger_filas(universo, TRANSPORTED_FORECAST_MODELS)
    for f in filas:  # la identidad viaja EN cada fila, no sólo en el recibo
        f.setdefault("campaign_id", campaign_id)
        f.setdefault("code_sha", code_sha)
        f.setdefault("panel_sha256", tf.panel_sha256_of(ROOT))

    recibo = tf.write(
        filas,
        esperado,
        root=ROOT,
        campaign_id=campaign_id,
        code_sha=code_sha,
        panel_sha256=tf.panel_sha256_of(ROOT),
    )
    print(f"✓ transporte producido y acreditado ({len(filas)} filas) → {recibo.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
