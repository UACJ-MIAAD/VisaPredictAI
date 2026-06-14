from datetime import datetime

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

from config import UACJ_AMARILLO, UACJ_AZUL, UACJ_GRIS, UACJ_NEGRO  # noqa: F401

TODAY = datetime(2026, 2, 13)

countries = ['India', 'China', 'Mexico', 'Philippines', 'RoW']
country_names_es = {
    'India': 'India', 'China': 'China', 'Mexico': 'M\u00e9xico',
    'Philippines': 'Filipinas', 'RoW': 'Resto del Mundo',
}
eb_levels = ['EB1', 'EB2', 'EB3', 'EB4']
eb_labels = [
    'EB-1 \u2014 Prioridad para trabajadores\ncon habilidades extraordinarias',
    'EB-2 \u2014 Profesionistas con grado\navanzado o habilidad excepcional',
    'EB-3 \u2014 Trabajadores calificados,\nprofesionistas y otros',
    'EB-4 \u2014 Inmigrantes especiales\n(religiosos, empleados de gobierno, etc.)',
]

for country in countries:
    df = pd.read_csv(f'data/{country.lower()}_visa_backlog_timecourse.csv')
    df['visa_bulletin_date'] = pd.to_datetime(df['visa_bulletin_date'])
    df['visa_wait_time'] = df['visa_wait_time'].astype(float)

    nombre = country_names_es[country]
    fig, axs = plt.subplots(2, 2, figsize=(12, 10))
    fig.suptitle(f'Tiempos de espera de visa por empleo \u2014 {nombre}',
                 fontsize=14, fontweight='bold', color=UACJ_NEGRO)

    # Only the Final Action Dates table feeds these wait-time curves; the CSV
    # now also carries Dates for Filing rows (table_type), which must be excluded.
    fad = df[df['table_type'] == 'final_action'] if 'table_type' in df.columns else df

    for i, (level, label) in enumerate(zip(eb_levels, eb_labels, strict=False)):
        ax = axs[i // 2, i % 2]
        data = fad[fad['EB_level'] == level]

        ax.plot(data['visa_bulletin_date'], data['visa_wait_time'],
                color=UACJ_AZUL, linewidth=1.5,
                label='Fechas de Acci\u00f3n Final')

        # "Hoy" reference line
        ax.axvline(x=TODAY, color=UACJ_GRIS, linestyle=':', linewidth=1,
                   label='Hoy', alpha=0.8)

        ax.xaxis.set_major_locator(mdates.YearLocator(2))
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y'))
        ax.tick_params(axis='x', rotation=45, colors=UACJ_NEGRO)
        ax.tick_params(axis='y', colors=UACJ_NEGRO)
        ax.set_title(label, fontweight='bold', color=UACJ_NEGRO, fontsize=10)
        ax.set_xlabel('Fecha del bolet\u00edn', color=UACJ_GRIS)
        ax.set_ylabel('Tiempo de espera (a\u00f1os)', color=UACJ_GRIS)
        ax.legend(fontsize=8)

    plt.tight_layout()
    plt.savefig(f'figures/{country}_visa_wait_times.png', dpi=150)
    plt.close()
