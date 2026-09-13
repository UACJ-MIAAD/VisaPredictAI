"""Ayudas para las pruebas que tocan `holdout_forecasts_*`.

★ **M74-E-R2 retiró el ayudante que sellaba fixtures sintéticos.** Desde R2 el lector RECALCULA el
conjunto esperado contra el panel real —25 series × 9 modelos × 24 meses— y lo vuelve a auditar, así
que un fixture de tres series **no puede acreditarse**, y eso es correcto: era justo el agujero que
dejaba pasar un CSV de una fila con un recibo que declaraba 5 400. Las pruebas de aritmética pasan
su marco por la costura `fc=`; las de integración usan el salto de abajo.

⚠️ El import del producto va DENTRO de la función: este archivo vive en `tests/` y el job base sólo
instala `.[dev]`. Es la trampa que este repositorio ha pisado ocho veces.
"""

from __future__ import annotations


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
