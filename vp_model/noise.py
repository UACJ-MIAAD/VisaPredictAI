"""Los avisos conocidos del ajuste de modelos, declarados en UN solo sitio.

Tres corredores silenciaban **todo** (`warnings.filterwarnings("ignore")`) mientras ajustaban
modelos: `auto_arima_baseline`, `freeze_shadow` y `generate_web_forecasts`. Eso convertía cada
campaña en una corrida ciega, justo donde importa ver.

Medido sobre 25 series reales del panel (SARIMAX y ETS amortiguado), el camino de ajuste emite
**exactamente dos familias**, ambas de arranque del optimizador, y ambas ya registradas en el
contrato de warnings de la suite (`security/warnings_registry.json`). Se añaden las dos de
convergencia, que el mismo contrato reconoce y que dependen del corredor.

**Por qué se filtra por mensaje y con `Warning` como categoría**, en vez de importar
`ConvergenceWarning`: este módulo lo importan corredores que corren en el job base, donde
`statsmodels` **no está instalado**. Importarlo aquí rompería la colección de pruebas (la
reincidencia de M14, M48, M59, M62 y M64). Los mensajes son literales y específicos, así que el
filtro sigue siendo estrecho: cualquier otro aviso, de la categoría que sea, llega a la superficie.
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Iterator
from contextlib import contextmanager

#: Prefijos EXACTOS de los avisos que el ajuste emite sobre este panel. Cada uno tiene su entrada
#: en `security/warnings_registry.json` con paquete, versión y fecha de revisión.
KNOWN_FIT_WARNINGS: tuple[str, ...] = (
    # medidos: 5 de 25 series (SARIMAX) · registro `statsmodels-nonstationary-ar-start`
    "Non-stationary starting autoregressive parameters found",
    # medidos: 3 de 25 series (SARIMAX) · registro `statsmodels-noninvertible-ma-start`
    "Non-invertible starting MA parameters found",
    # registro `statsmodels-mle-convergence`
    "Maximum Likelihood optimization failed to converge",
    # registro `statsmodels-holtwinters-convergence`
    "Optimization failed to converge",
)


@contextmanager
def silenced_fit_warnings() -> Iterator[None]:
    """Silencia SÓLO los avisos conocidos del ajuste, y nada más.

    Se restaura al salir, también si el cuerpo lanza. Lo que este gestor **no** cubre llega a la
    superficie: ése es el punto de haber retirado las supresiones amplias.
    """
    with warnings.catch_warnings():
        for prefijo in KNOWN_FIT_WARNINGS:
            warnings.filterwarnings("ignore", message=re.escape(prefijo), category=Warning)
        yield
