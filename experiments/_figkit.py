"""Kit común de figuras (C5): estado explícito e inmutable para idioma y tema.

Antes, `make_gallery_figures` y `make_fe_figures` llevaban el idioma y el tema en
globals de módulo que `_apply_lang`/`_apply_theme` re-vinculaban antes de cada pasada.
Funcionaba mientras nadie intercalara contextos, pero el estado era invisible en la
firma de cada figura: una pasada EN/oscura podía contaminar a la siguiente ES/clara
dentro del mismo proceso, los reportes tenían que llamar a esas funciones privadas
para «poner» el idioma, y los rcParams quedaban modificados al terminar.

Aquí el contexto viaja como argumento y no se muta: `Theme` y `LangCtx` son
inmutables, `FigureContext` los combina y sabe qué variante es y dónde escribe, y
`figure_style` aplica los rcParams de forma reversible incluso si algo revienta.

El kit no sabe nada de ciencia: ni datos, ni cifras, ni títulos, ni qué series se
dibujan. Eso vive en cada generador.
"""

from __future__ import annotations

import textwrap
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

import matplotlib.pyplot as plt
import pandas as pd

__all__ = [
    "BASE_RC",
    "Theme",
    "LangCtx",
    "FigureContext",
    "VARIANTS",
    "figure_style",
    "plain_style",
    "save_dual",
    "run_variants",
    "header",
    "footer",
    "num",
]

# Orden determinista de las cuatro pasadas. Es el mismo que tenía el bucle manual y
# el que espera la equivalencia visual: cambiarlo cambiaría qué figura se dibuja con
# qué rcParams heredados si alguna vez volviera a haber estado compartido.
VARIANTS: tuple[tuple[str, str], ...] = (("es", "light"), ("es", "dark"), ("en", "light"), ("en", "dark"))


# Marco visual comun de todas las figuras del proyecto. Antes cada generador lo aplicaba
# al importarse (`style()` + un `rcParams.update` suelto), asi que importar un modulo
# tenia el efecto lateral de reconfigurar Matplotlib para todo el proceso.
BASE_RC: Mapping[str, Any] = {
    "font.family": "serif",
    "font.size": 10,
    "savefig.bbox": "tight",
    "savefig.dpi": 300,
}

THEMES: frozenset[str] = frozenset({"light", "dark"})
LANGS: frozenset[str] = frozenset({"es", "en"})


@dataclass(frozen=True)
class Theme:
    """Un tema de figura: nombre estable y los colores que le da la paleta.

    Los colores llegan ya resueltos desde `vp_model.palette` (fuente única); el tema
    no los inventa ni los recalcula. Construirlo no toca ningún estado global.

    `overrides` deja que una familia de figuras fije lo suyo (tamaños, rejilla) sin
    tocar rcParams globales: se aplican dentro del contexto y se van con él.
    """

    name: str
    colors: Mapping[str, Any]
    overrides: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.name not in THEMES:
            raise ValueError(f"tema desconocido: {self.name!r} (esperado uno de {sorted(THEMES)})")
        # Copias inmutables: quien nos paso el diccionario no puede mutarnos despues.
        object.__setattr__(self, "colors", MappingProxyType(dict(self.colors)))
        object.__setattr__(self, "overrides", MappingProxyType(dict(self.overrides)))

    @property
    def dark(self) -> bool:
        return self.name == "dark"

    def pick(self, *names: str) -> tuple[Any, ...]:
        """Los colores pedidos, en orden. Falla claro si a la paleta le falta uno."""
        missing = [n for n in names if n not in self.colors]
        if missing:
            raise KeyError(f"el tema {self.name!r} no define {missing}")
        return tuple(self.colors[n] for n in names)

    @property
    def rcparams(self) -> Mapping[str, Any]:
        """El marco visual completo de esta variante: base + colores + overrides.

        Es un mapping propio y se aplica dentro de `figure_style`: nadie muta el
        rcParams de Matplotlib de forma permanente.
        """
        paper, ink, gray, mid, grid, blue = self.pick("PAPER", "INK", "GRAY", "MID", "GRID", "BLUE")
        return {
            **BASE_RC,
            "figure.facecolor": paper,
            "axes.facecolor": paper,
            "savefig.facecolor": paper,
            "axes.edgecolor": mid,
            "axes.labelcolor": ink,
            "axes.titlecolor": blue,
            "xtick.color": gray,
            "ytick.color": gray,
            "text.color": ink,
            "grid.color": grid,
            **self.overrides,
        }


@dataclass(frozen=True)
class LangCtx:
    """El idioma de una pasada: código, textos visibles, meses y países.

    `countries` es opcional porque la galería FE no rotula países; pedirlo cuando no
    existe es un error del llamador, no un `None` que se cuele hasta el dibujo.
    """

    code: str
    txt: Mapping[str, Any]
    months: Mapping[int, str]
    countries: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if self.code not in LANGS:
            raise ValueError(f"idioma desconocido: {self.code!r} (esperado uno de {sorted(LANGS)})")
        object.__setattr__(self, "txt", MappingProxyType(dict(self.txt)))
        object.__setattr__(self, "months", MappingProxyType(dict(self.months)))
        if self.countries is not None:
            object.__setattr__(self, "countries", MappingProxyType(dict(self.countries)))

    @property
    def cname(self) -> Mapping[str, str]:
        if self.countries is None:
            raise AttributeError(f"el contexto {self.code!r} no define nombres de país")
        return self.countries


@dataclass(frozen=True)
class FigureContext:
    """Tema + idioma: todo lo que una figura necesita saber de su pasada.

    No conoce cifras, ni rutas concretas de EDA o FE: solo qué variante es y en qué
    subdirectorio relativo le toca escribir.
    """

    theme: Theme
    lang: LangCtx

    @property
    def variant(self) -> str:
        return f"{self.lang.code}/{self.theme.name}"

    @property
    def subdir(self) -> Path:
        """Subdirectorio relativo de la variante: es/light escribe en la raíz."""
        parts = [p for p in (("en" if self.lang.code == "en" else ""), ("dark" if self.theme.dark else "")) if p]
        return Path(*parts) if parts else Path()

    @property
    def academic(self) -> bool:
        """ES/claro es la única variante del entregable académico: la que emite PDF."""
        return self.lang.code == "es" and not self.theme.dark


@contextmanager
def plain_style(rcparams: Mapping[str, Any]) -> Iterator[None]:
    """Aplica un conjunto de rcParams tal cual y los restaura al salir.

    Para las figuras que NO tienen variantes de idioma ni tema —los resultados del
    entregable, monolingües y solo PDF— y cuyo marco visual es el suyo, no el de la web.
    Envolverlas en un `Theme` completo les cambiaría el color de título, texto y ejes: el
    tema web define semánticas de color que ese generador nunca aplicó.
    """
    with plt.rc_context(dict(rcparams)):
        yield


@contextmanager
def figure_style(ctx: FigureContext) -> Iterator[FigureContext]:
    """Aplica los rcParams del tema y los restaura al salir, pase lo que pase.

    Esto es lo que `_apply_theme` no hacía: mutaba el rcParams global y lo dejaba
    mutado, así que el estado de una pasada sobrevivía a la siguiente y al proceso.
    """
    with plt.rc_context(dict(ctx.theme.rcparams)):
        yield ctx


def save_dual(
    fig: plt.Figure,
    name: str,
    ctx: FigureContext,
    *,
    png_root: Path,
    pdf_root: Path,
    pdf_name: Callable[[str], str | None] | None = None,
    log_name: Callable[[str], str] | None = None,
    announce: bool = True,
) -> plt.Figure:
    """Guarda la figura donde le toca a su variante y DEVUELVE la figura viva.

    ES/claro escribe el PNG de 300 dpi y, si `pdf_name` lo autoriza para ese nombre,
    el PDF vectorial del `.tex`. Las otras tres variantes son solo web: un PNG en su
    subdirectorio. La figura **no se cierra**: pertenece al llamador, y los reportes
    la reutilizan tal cual como página vectorial (cero re-rasterización).
    """
    if not ctx.academic:
        sub = png_root / ctx.subdir
        sub.mkdir(parents=True, exist_ok=True)
        (paper,) = ctx.theme.pick("PAPER")
        fig.savefig(sub / f"{name}.png", bbox_inches="tight", dpi=300, facecolor=paper)
        if announce:
            print(f"{sub.relative_to(png_root)}/{name} OK")
        return fig
    png_root.mkdir(parents=True, exist_ok=True)
    if pdf_name is not None:
        target = pdf_name(name)
        if target is not None:
            fig.savefig(pdf_root / f"{target}.pdf", bbox_inches="tight")
    fig.savefig(png_root / f"{name}.png", bbox_inches="tight", dpi=300)
    if announce:
        # `log_name` es cómo se ANUNCIA la figura, que no siempre coincide con el PDF que
        # emite: la galería EDA rotula todas sus figuras `eda3_*` aunque solo seis tengan
        # PDF. Cambiar esa línea sería alterar la salida del cron sin declararlo.
        print(f"{(log_name or pdf_name or str)(name) or name} OK")
    return fig


def run_variants(
    makers: Sequence[Callable[..., plt.Figure]],
    context_for: Callable[[str, str], FigureContext],
    *args: Any,
    variants: Iterable[tuple[str, str]] = VARIANTS,
) -> None:
    """Ejecuta cada maker una vez por variante, en orden determinista.

    Cada figura se cierra tras usarse y los rcParams se restauran aunque un maker
    falle: la excepción se propaga sin tragarse, pero no deja el proceso teñido.
    """
    for lang_code, theme_name in variants:
        ctx = context_for(lang_code, theme_name)
        with figure_style(ctx):
            for maker in makers:
                plt.close(maker(*args, ctx))


def num(v: int) -> str:
    """Separador de miles con coma (27,611): convencion es-MX del proyecto, igual en
    ambos idiomas. El espacio fino previo desalineaba la figura frente al caption/.tex."""
    return f"{v:,}"


def header(fig: plt.Figure, headline: str, sub: str, ctx: FigureContext, y: float = 1.02, dy: float = 0.055) -> None:
    """Titular-frase (el hallazgo) + bajada explicativa, estilo editorial.

    Envuelve titular y bajada al ancho de la figura (sin corte de carro, una bajada
    larga estiraba el bbox y encogia el plot). Las lineas extra crecen hacia ARRIBA
    (va="bottom" + lift del titular) para no invadir los ejes.
    """
    gray, ink = ctx.theme.pick("GRAY", "INK")
    w_in, h_in = fig.get_size_inches()
    head = textwrap.fill(headline, width=max(28, int(w_in * 7.2)))
    body = textwrap.fill(sub, width=max(50, int(w_in * 12.5)))
    lift = body.count(chr(10)) * (0.175 / h_in)
    fig.text(0.01, y + dy + lift, head, fontsize=14, fontweight="bold", color=ink, ha="left", va="bottom")
    fig.text(0.01, y, body, fontsize=9.5, color=gray, ha="left", va="bottom")


def footer(fig: plt.Figure, vintage: str, ctx: FigureContext, extra: str = "", y: float = -0.045) -> None:
    """Pie con la anada del corte y la marca, en el idioma del contexto."""
    blue, gray = ctx.theme.pick("BLUE", "GRAY")
    per = pd.Period(vintage)
    text = ctx.lang.txt["footer"].format(mes=ctx.lang.months[per.month], anio=per.year) + (
        f"  {extra}" if extra else ""
    )
    fig.text(0.01, y, text, fontsize=7.4, color=gray, ha="left")
    # con pie largo la marca baja un renglon para no encimarse con el texto
    brand_y = y - 0.035 if len(text) > 80 else y
    fig.text(0.99, brand_y, "VisaPredict AI", fontsize=8.5, color=blue, ha="right", fontweight="bold")
