"""La superficie de avisos de Zivot-Andrews, medida y FIJADA (H11 de la auditoría ciega).

M72 declaró «4 series de 114»; la auditoría ciega midió 5; la sonda exacta da **6 de 116**. Las tres
cifras salían de universos distintos, y ninguna estaba fijada por una prueba: por eso podían
divergir sin que nadie lo notara.

Aquí se fija **el universo** —catálogo completo con ≥24 observaciones tras ``_clean``, que **no** son
las 74 evaluables— y **los dos mensajes** que el filtro estrecho de
``series_characterization.advanced`` debe cubrir. Si mañana aparece una serie nueva, o un tercer
mensaje, esta prueba lo dice en vez de dejar que el contrato ``error`` de la suite reviente en la
primera prueba que toque empleo.
"""

from __future__ import annotations

import os
import warnings
from importlib.util import find_spec

import pytest

MODELADO = pytest.mark.skipif(find_spec("statsmodels") is None, reason="requiere el extra `model`")

#: Las series cuyo Zivot-Andrews cae en aritmética no finita, medidas sobre el universo declarado.
SERIES_AFECTADAS: dict[str, tuple[str, ...]] = {
    "all_chargeability/EB2/DFF": ("invalid value encountered in sqrt",),
    "china/EB5_TEA/FAD": ("invalid value encountered in sqrt",),
    "india/EB5_UNRESERVED/FAD": ("invalid value encountered in sqrt",),
    "mexico/EB2/DFF": ("invalid value encountered in sqrt",),
    "mexico/EB4_RW/DFF": ("divide by zero encountered in divide", "invalid value encountered in sqrt"),
    "philippines/EB2/DFF": ("invalid value encountered in sqrt",),
}
#: Los mensajes DISTINTOS que el filtro debe cubrir. Son dos, no uno.
MENSAJES = tuple(sorted({m for v in SERIES_AFECTADAS.values() for m in v}))
MIN_OBS = 24


def _universo():
    """El universo EXACTO de la medición: catálogo completo con ≥24 obs tras `_clean`."""
    pytest.importorskip("statsmodels")  # ayudante de módulo: se protege solo (job base)
    from vp_model import dataset
    from vp_model import series_characterization as sc

    for r in dataset.list_series().itertuples():
        clave = (r.country, r.category, r.table)
        try:
            s = sc._clean(*clave)
        except Exception:  # noqa: BLE001 — broad-catch: una serie ilegible no define la superficie
            continue
        if len(s.dropna()) >= MIN_OBS:
            yield clave, s


BARRIDO_COMPLETO = os.environ.get("VP_FULL_SWEEP") == "1"


@MODELADO
@pytest.mark.slow
@pytest.mark.skipif(not BARRIDO_COMPLETO, reason="barrido completo: VP_FULL_SWEEP=1 (≈2-3 min)")
def test_the_measured_warning_surface_is_exactly_the_pinned_one() -> None:
    """Barrido COMPLETO del universo real contra la superficie fijada. Opt-in por coste.

    Es el que detectaría una serie NUEVA que empiece a avisar. La comprobación barata de abajo
    cubre el otro sentido —que las fijadas sigan avisando lo mismo— y sí corre siempre.
    """
    from statsmodels.tsa.stattools import zivot_andrews

    medido: dict[str, tuple[str, ...]] = {}
    n = 0
    for clave, s in _universo():
        n += 1
        diff = s.diff().dropna()
        with warnings.catch_warnings(record=True) as reg:
            warnings.simplefilter("always")
            try:
                zivot_andrews(diff, trim=0.15)
            except Exception:  # noqa: BLE001 — broad-catch: el fallo es una convención declarada (NaN)
                pass
        msgs = tuple(sorted({str(w.message) for w in reg}))
        if msgs:
            medido["/".join(clave)] = msgs

    assert n >= 100, f"el universo medido encogió a {n}: la cifra fijada ya no es comparable"
    assert medido == SERIES_AFECTADAS, (
        "la superficie de avisos de Zivot-Andrews cambió.\n"
        f"  medido: {medido}\n  fijado: {SERIES_AFECTADAS}\n"
        "Si es legítimo, actualiza SERIES_AFECTADAS **y** el filtro de series_characterization."
    )


@MODELADO
def test_the_pinned_series_still_emit_exactly_what_was_pinned() -> None:
    """Barato y siempre: las 6 series fijadas, una por una, con sus mensajes exactos.

    No sustituye al barrido completo (no ve series nuevas), pero sí caza que una fijada cambie
    de mensaje o deje de emitir — que es como la cifra de M72 se volvió falsa sin que nadie lo viera.
    """
    from statsmodels.tsa.stattools import zivot_andrews

    from vp_model import series_characterization as sc

    for clave, esperados in SERIES_AFECTADAS.items():
        pais, categoria, tabla = clave.split("/")
        s = sc._clean(pais, categoria, tabla)
        diff = s.diff().dropna()
        with warnings.catch_warnings(record=True) as reg:
            warnings.simplefilter("always")
            try:
                zivot_andrews(diff, trim=0.15)
            except Exception:  # noqa: BLE001 — broad-catch: el fallo es la convención declarada (NaN)
                pass
        medido = tuple(sorted({str(w.message) for w in reg}))
        assert medido == esperados, f"{clave}: medido {medido}, fijado {esperados}"


@MODELADO
def test_the_narrow_filter_covers_every_measured_message() -> None:
    """El filtro de `advanced` cubre los DOS mensajes — y nada más ancho que ellos."""
    import inspect

    from vp_model import series_characterization as sc

    fuente = inspect.getsource(sc.advanced)
    for mensaje in MENSAJES:
        assert mensaje in fuente, f"el filtro no cubre {mensaje!r}, que sí se emite"
    assert "category=RuntimeWarning" in fuente, "el filtro debe seguir acotado por categoría"


@MODELADO
def test_a_foreign_runtime_warning_still_escapes(monkeypatch: pytest.MonkeyPatch) -> None:
    """RED: cubrir dos mensajes no puede volverse cubrir la categoría entera."""
    import statsmodels.tsa.stattools as st

    from vp_model import series_characterization as sc

    monkeypatch.setattr(sc, "_clean", lambda *a, **k: _serie())
    ajeno = "overflow encountered in exp"

    def za(*args, **kwargs):
        warnings.warn(ajeno, RuntimeWarning, stacklevel=2)
        raise ValueError("corte")

    monkeypatch.setattr(st, "zivot_andrews", za)
    with warnings.catch_warnings(record=True) as reg:
        warnings.simplefilter("always")
        sc.advanced("mexico", "EB2", "FAD")
    assert ajeno in [str(w.message) for w in reg], "un RuntimeWarning ajeno debe seguir escapando"


def _serie():
    import numpy as np
    import pandas as pd

    idx = pd.date_range("2015-01-01", periods=80, freq="MS")
    rng = np.random.default_rng(20260911)
    return pd.Series(np.arange(80, dtype="float64") * 30.0 + rng.normal(0, 5, 80), index=idx)
