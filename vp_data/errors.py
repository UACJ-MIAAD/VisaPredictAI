"""Errores tipados de la capa de datos (C4).

Autoridad única de las excepciones que el pipeline sabe nombrar. Existe para
separar dos cosas que ``except Exception`` confundía:

* un **fallo esperado** de la fuente o del formato — la red falla, el WAF nos
  bloquea, un boletín trae una tabla que no se deja parsear — que se agrega al
  reporte mensual y deja al resto del panel avanzar;
* un **defecto de programación** — un nombre mal escrito, un tipo imposible —
  que debe propagarse y poner el proceso en rojo en vez de disfrazarse de «mes
  perdido» en una lista de fallos.

``ParseError`` conserva la causa (``raise ... from exc``) y el contexto
estructurado del sitio donde ocurrió, de modo que el reporte mensual pueda decir
país y mes en vez de un ``str(exc)[:60]`` recortado.
"""

from __future__ import annotations

__all__ = ["FetchError", "ParseError", "SourceBlockedError", "PARSE_FAILURES"]


class FetchError(Exception):
    """A failed fetch. ``permanent`` tells ``with_retry`` whether retrying can help."""

    def __init__(self, url: str, msg: str, *, status: int | None = None, permanent: bool = False):
        super().__init__(f"{msg} [{url}]")
        self.url = url
        self.status = status
        self.permanent = permanent


class SourceBlockedError(FetchError):
    """The source's WAF/anti-bot layer refused us (Cloudflare). Permanent for
    this run: no amount of backoff clears a challenge page, so consumers must
    degrade (record the block, exit clean) instead of retrying or failing red."""

    def __init__(self, url: str, msg: str = "fuente tras el WAF (Cloudflare)", *, status: int | None = None):
        super().__init__(url, msg, status=status, permanent=True)


class ParseError(Exception):
    """Un boletín que no se deja leer. Es un fallo ESPERADO del formato, no un bug.

    Lleva el contexto del sitio donde ocurrió para que el reporte mensual nombre
    la etapa, la fuente y, cuando se conozcan, el mes y el país. La causa original
    viaja en ``__cause__``: nunca se pierde, siempre se levanta con ``from``.
    """

    def __init__(
        self,
        stage: str,
        source: str,
        msg: str,
        *,
        month: str | None = None,
        country: str | None = None,
    ):
        self.stage = stage
        self.source = source
        self.msg = msg
        self.month = month
        self.country = country
        super().__init__(self.describe())

    def describe(self) -> str:
        """Una línea con todo el contexto que se conoce, sin recortar el mensaje."""
        bits = [f"etapa={self.stage}", f"fuente={self.source}"]
        if self.month:
            bits.append(f"mes={self.month}")
        if self.country:
            bits.append(f"país={self.country}")
        return f"{self.msg} ({', '.join(bits)})"

    @property
    def context(self) -> dict[str, str | None]:
        """El contexto estructurado, para quien quiera algo más que la línea."""
        return {"stage": self.stage, "source": self.source, "month": self.month, "country": self.country}


# Los fallos que un boletín mal formado produce de verdad: una tabla que no está,
# una columna que falta, una fecha imposible, bytes que no decodifican, un archivo
# que no se puede leer. Cualquier otra excepción (AttributeError, TypeError,
# NameError, RuntimeError…) es un defecto nuestro y NO se captura aquí a propósito:
# tiene que escapar y poner el proceso en rojo.
PARSE_FAILURES = (ValueError, KeyError, IndexError, ArithmeticError, UnicodeError, OSError)
