"""Paleta cromática canónica del proyecto — fuente única de verdad para TODAS las figuras.

Antes, cada script de figuras definía sus propios colores (10+ grises distintos, 3 vinos,
3 dorados, 4 mapas de calor diferentes, y colores de país inconsistentes entre figuras).
Este módulo unifica todo sobre la identidad UACJ para lograr consistencia y legibilidad.

Uso:  from vp_model.palette import BLUE, GRAY, COUNTRY, REGIME, SEQ, WARN, DIV, style
      style()  # aplica rcParams comunes (serif, ejes, grid, dpi) a matplotlib
"""

from __future__ import annotations

from matplotlib.colors import LinearSegmentedColormap

# --- Núcleo institucional UACJ --------------------------------------------------------
BLUE = "#003CA6"  # uacjblue — color primario
YELLOW = "#FFD600"  # uacjyellow — solo rellenos/chips (ilegible como línea fina)
GRAY = "#555559"  # uacjgray — texto secundario, ejes
INK = "#231F20"  # uacjblack — texto/línea dominante

# --- Rampa de grises fríos (consolida los 10+ grises dispersos) ------------------------
MID = "#9AA3AD"  # gris medio: marcadores secundarios, líneas de referencia
MUTE = "#C4CAD2"  # series/barras atenuadas (inactivas)
GRID = "#E8ECF0"  # líneas de cuadrícula
STRIPE = "#F4F6FB"  # cebra de tablas

# --- Acentos secundarios (una sola versión de cada uno) --------------------------------
TEAL = "#2E7D6F"
WINE = "#8C2D2D"
GOLD = "#B8860B"
SLATE = "#5B6770"

# --- Colores de país/área (CONSISTENTES en todo el documento) -------------------------
# México = azul (sujeto piloto, coincide con el resaltado Latinometrics). Tonos bien
# separados en matiz (azul/vino/dorado/teal/pizarra) y razonablemente daltónico-distintos.
COUNTRY = {
    "mexico": BLUE,
    "india": WINE,
    "china": GOLD,
    "philippines": TEAL,
    "all_chargeability": SLATE,
}
COUNTRY_NAME = {
    "mexico": "México",
    "india": "India",
    "china": "China",
    "philippines": "Filipinas",
    "all_chargeability": "Resto del mundo",
}

# --- Régimen de celda C/F/U/UNK (relleno pastel para tablas, línea sólida para series) -
REGIME = {
    "F": {"fill": "#DCE6F5", "line": BLUE},  # fecha publicada (objetivo)
    "C": {"fill": "#D8EAD3", "line": TEAL},  # Current (sin atraso)
    "U": {"fill": "#F6D9D6", "line": WINE},  # Unavailable
    "UNK": {"fill": "#ECECEC", "line": MID},  # sin dato
}

# --- Mapas de calor de marca (reemplazan vlag/crest/YlGnBu/YlOrRd) --------------------
SEQ = LinearSegmentedColormap.from_list("uacj_seq", ["#FFFFFF", "#9CC0F0", BLUE])  # magnitud neutra
WARN = LinearSegmentedColormap.from_list("uacj_warn", ["#FFFFFF", "#E0A6A0", WINE])  # magnitud "malo" (error)
DIV = LinearSegmentedColormap.from_list("uacj_div", [WINE, "#FFFFFF", BLUE])  # divergente (correlación)


def country_color(c: str) -> str:
    """Color de un país; gris pizarra por defecto para claves desconocidas."""
    return COUNTRY.get(c, SLATE)


def style() -> None:
    """Aplica rcParams comunes a todas las figuras (marco visual idéntico)."""
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "font.family": "serif",
            "font.size": 10,
            "axes.edgecolor": MID,
            "axes.labelcolor": INK,
            "axes.titlecolor": BLUE,
            "xtick.color": GRAY,
            "ytick.color": GRAY,
            "text.color": INK,
            "grid.color": GRID,
            "savefig.bbox": "tight",
            "savefig.dpi": 300,
        }
    )
