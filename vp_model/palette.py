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

# --- Neutros del tema CLARO (AE3: antes hardcodeados en make_gallery_figures) ----------
# Espejo exacto de las claves de DARK para que _apply_theme() pueda re-vincular en ambas
# direcciones desde UNA fuente. UNK_FILL reconciliado con REGIME["UNK"]["fill"] — había
# DOS grises "sin dato" distintos (#EFEFEF vs #ECECEC) en el mismo documento.
LIGHT: dict = {
    "PAPER": "#FFFFFF",
    "INK": INK,
    "GRAY": GRAY,
    "MID": MID,
    "MUTE": MUTE,
    "GRID": GRID,
    "STRIPE": STRIPE,
    "BLUE": BLUE,
    "TEAL": TEAL,
    "WINE": WINE,
    "GOLD": GOLD,
    "SLATE": SLATE,
    "UNK_FILL": REGIME["UNK"]["fill"],  # un solo gris "sin dato"
    "NODATA": "#D9D9D9",
    "QUAD_BLUE": REGIME["F"]["fill"],  # mismo azul pastel que las celdas F
    "COUNTRY": COUNTRY,
    "SEQ": SEQ,
    "DIV": DIV,
}

# --- Variante OSCURA (única fuente del dark mode de figuras web) -----------------------
# Mismo lenguaje cromático sobre superficie charcoal (alineada al dark del sitio). Los
# tonos de dato se ACLARAN para conservar contraste; el amarillo UACJ no cambia. Usar
# SOLO para artefactos que se muestran en pantalla oscura (galería web); el .tex y el
# reporte PDF siguen en la paleta clara.
DARK: dict = {
    "PAPER": "#12161B",  # superficie de la figura (charcoal, no negro puro)
    "INK": "#E8EAED",
    "GRAY": "#A9B1BA",
    "MID": "#7A8590",
    "MUTE": "#414B56",
    "GRID": "#262D34",
    "STRIPE": "#1A2027",
    "BLUE": "#7AA7F8",
    "TEAL": "#57BFAA",
    "WINE": "#E08B84",
    "GOLD": "#D9A93D",
    "SLATE": "#97A5B2",
    "UNK_FILL": "#262C33",  # celdas U/sin dato en matrices
    "NODATA": "#333A42",  # barras "sin dato"
    "QUAD_BLUE": "#1E2A3F",  # cuadrante sombreado (censo de estacionariedad)
    "COUNTRY": {
        "mexico": "#7AA7F8",
        "india": "#E08B84",
        "china": "#D9A93D",
        "philippines": "#57BFAA",
        "all_chargeability": "#97A5B2",
    },
    "SEQ": LinearSegmentedColormap.from_list("uacj_seq_dark", ["#1A222E", "#3D5C93", "#7AA7F8"]),
    "DIV": LinearSegmentedColormap.from_list("uacj_div_dark", ["#E08B84", "#1E242B", "#7AA7F8"]),
}


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
