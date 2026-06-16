"""Plot visa wait times per country from ``data/raw/`` into publication-ready
PNGs under ``figures/`` (not versioned; regenerate on demand).

One script, parameterized by ``block`` (employment / family): the two blocks
differ only in their category levels/labels, subplot grid, and whether a Dates
for Filing curve is drawn — everything else (palette, axes, "Hoy" line, output
naming) is shared.

    ante/bin/python visualize_wait_times.py
"""

from datetime import datetime

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

from config import UACJ_AMARILLO, UACJ_AZUL, UACJ_GRIS, UACJ_NEGRO

TODAY = datetime(2026, 2, 13)

COUNTRIES = ["India", "China", "Mexico", "Philippines", "RoW"]
COUNTRY_NAMES_ES = {
    "India": "India",
    "China": "China",
    "Mexico": "México",
    "Philippines": "Filipinas",
    "RoW": "Resto del Mundo",
}

# Per-block configuration. ``suffix`` selects both the source CSV
# (``{country}{suffix}_visa_backlog_timecourse.csv``) and the output PNG
# (``{country}{suffix}_visa_wait_times.png``). ``plot_dff`` adds the Dates for
# Filing curve (family only; the employment curves use Final Action only).
BLOCKS = [
    {
        "suffix": "",
        "title": "Tiempos de espera de visa por empleo",
        "level_col": "EB_level",
        "grid": (2, 2),
        "figsize": (12, 10),
        "plot_dff": False,
        "levels": ["EB1", "EB2", "EB3", "EB4"],
        "labels": [
            "EB-1 — Prioridad para trabajadores\ncon habilidades extraordinarias",
            "EB-2 — Profesionistas con grado\navanzado o habilidad excepcional",
            "EB-3 — Trabajadores calificados,\nprofesionistas y otros",
            "EB-4 — Inmigrantes especiales\n(religiosos, empleados de gobierno, etc.)",
        ],
    },
    {
        "suffix": "_family",
        "title": "Tiempos de espera de visa familiar",
        "level_col": "F_level",
        "grid": (3, 2),
        "figsize": (12, 12),
        "plot_dff": True,
        "levels": ["1", "2A", "2B", "3", "4"],
        "labels": [
            "F1 — Hijos(as) solteros de ciudadanos",
            "F2A — Esposos(as) e hijos menores\nde residentes permanentes",
            "F2B — Hijos(as) solteros (21+)\nde residentes permanentes",
            "F3 — Hijos(as) casados\nde ciudadanos",
            "F4 — Hermanos(as)\nde ciudadanos adultos",
        ],
    },
]


def plot_block(block: dict) -> None:
    nrows, ncols = block["grid"]
    for country in COUNTRIES:
        df = pd.read_csv(f"data/raw/{country.lower()}{block['suffix']}_visa_backlog_timecourse.csv")
        df["visa_bulletin_date"] = pd.to_datetime(df["visa_bulletin_date"])
        df["visa_wait_time"] = df["visa_wait_time"].astype(float)

        nombre = COUNTRY_NAMES_ES[country]
        fig, axs = plt.subplots(nrows, ncols, figsize=block["figsize"])
        fig.suptitle(f"{block['title']} — {nombre}", fontsize=14, fontweight="bold", color=UACJ_NEGRO)

        for i, (level, label) in enumerate(zip(block["levels"], block["labels"], strict=False)):
            ax = axs[i // ncols, i % ncols]
            level_data = df[df[block["level_col"]] == level]

            # Final Action Dates table (the CSV also carries Dates for Filing rows,
            # tagged in table_type, which must be excluded from the FAD curve).
            fad = level_data[level_data["table_type"] == "final_action"]
            if not fad.empty:
                ax.plot(
                    fad["visa_bulletin_date"],
                    fad["visa_wait_time"],
                    color=UACJ_AZUL,
                    linewidth=1.5,
                    label="Fechas de Acción Final",
                )
            if block["plot_dff"]:
                dff = level_data[level_data["table_type"] == "dates_for_filing"]
                if not dff.empty:
                    ax.plot(
                        dff["visa_bulletin_date"],
                        dff["visa_wait_time"],
                        color=UACJ_AMARILLO,
                        linestyle="--",
                        linewidth=1.5,
                        label="Fechas para Aplicar",
                    )

            # "Hoy" reference line
            ax.axvline(x=TODAY, color=UACJ_GRIS, linestyle=":", linewidth=1, label="Hoy", alpha=0.8)

            ax.xaxis.set_major_locator(mdates.YearLocator(2))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
            ax.tick_params(axis="x", rotation=45, colors=UACJ_NEGRO)
            ax.tick_params(axis="y", colors=UACJ_NEGRO)
            ax.set_title(label, fontweight="bold", color=UACJ_NEGRO, fontsize=10)
            ax.set_xlabel("Fecha del boletín", color=UACJ_GRIS)
            ax.set_ylabel("Tiempo de espera (años)", color=UACJ_GRIS)
            ax.legend(fontsize=8)

        # Hide any trailing subplots beyond the category count (e.g. 5 family
        # categories in a 3x2 grid leave one empty slot).
        for j in range(len(block["levels"]), nrows * ncols):
            axs[j // ncols, j % ncols].set_visible(False)

        plt.tight_layout()
        plt.savefig(f"figures/{country}{block['suffix']}_visa_wait_times.png", dpi=150)
        plt.close()


if __name__ == "__main__":
    for block in BLOCKS:
        plot_block(block)
