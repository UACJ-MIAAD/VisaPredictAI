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

import json
import re
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

#: Ids del registro cuyos mensajes silencia el ajuste. **Los prefijos NO se copian aquí**: se leen
#: de `security/warnings_registry.json`, que es su autoridad.
#:
#: ⚠️ Antes estaban transcritos y **recortados** (31 de 51 caracteres, 50 de 69, 55 de 92, 43 de 80).
#: Con `category=Warning`, un prefijo corto silencia variantes que el contrato de la suite exige NO
#: silenciar: el recorte convertía un filtro estrecho en uno más ancho que su propia entrada.
FIT_WARNING_IDS: tuple[str, ...] = (
    "statsmodels-nonstationary-ar-start",  # medidos: 5 de 25 series (SARIMAX)
    "statsmodels-noninvertible-ma-start",  # medidos: 3 de 25 series (SARIMAX)
    "statsmodels-mle-convergence",
    "statsmodels-holtwinters-convergence",
)

_REGISTRY = Path(__file__).resolve().parent.parent / "security" / "warnings_registry.json"


@lru_cache(maxsize=1)
def known_fit_warnings() -> tuple[str, ...]:
    """Los prefijos ÍNTEGROS de `FIT_WARNING_IDS`, leídos del registro. Falla cerrado."""
    entradas = {e["id"]: e["message_prefix"] for e in json.loads(_REGISTRY.read_text(encoding="utf-8"))["warnings"]}
    faltan = [i for i in FIT_WARNING_IDS if i not in entradas]
    if faltan:
        raise RuntimeError(f"vp_model.noise: ids ausentes del registro de warnings: {faltan}")
    return tuple(entradas[i] for i in FIT_WARNING_IDS)


@contextmanager
def silenced_fit_warnings() -> Iterator[None]:
    """Silencia SÓLO los avisos conocidos del ajuste, y nada más.

    Se restaura al salir, también si el cuerpo lanza. Lo que este gestor **no** cubre llega a la
    superficie: ése es el punto de haber retirado las supresiones amplias.
    """
    with warnings.catch_warnings():
        for prefijo in known_fit_warnings():
            warnings.filterwarnings("ignore", message=re.escape(prefijo), category=Warning)
        yield
