import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd
from datetime import datetime


# UACJ / MIAAD institutional colors
UACJ_AZUL = '#003CA6'
UACJ_AMARILLO = '#FFD600'
UACJ_GRIS = '#555559'
UACJ_NEGRO = '#231F20'

TODAY = datetime(2026, 2, 13)

countries = ['India', 'China', 'Mexico', 'Philippines', 'RoW']
country_names_es = {
    'India': 'India', 'China': 'China', 'Mexico': 'Mexico',
    'Philippines': 'Filipinas', 'RoW': 'Resto del Mundo',
}
f_levels = ['1', '2A', '2B', '3', '4']
f_labels = [
    'F1 — Hijos(as) solteros de ciudadanos',
    'F2A — Esposos(as) e hijos menores\nde residentes permanentes',
    'F2B — Hijos(as) solteros (21+)\nde residentes permanentes',
    'F3 — Hijos(as) casados\nde ciudadanos',
    'F4 — Hermanos(as)\nde ciudadanos adultos',
]

for country in countries:
    df = pd.read_csv(f'data/{country.lower()}_family_visa_backlog_timecourse.csv')
    df['visa_bulletin_date'] = pd.to_datetime(df['visa_bulletin_date'])
    df['visa_wait_time'] = df['visa_wait_time'].astype(float)

    fig, axs = plt.subplots(3, 2, figsize=(12, 12))
    nombre = country_names_es[country]
    fig.suptitle(f'Tiempos de espera de visa familiar — {nombre}',
                 fontsize=14, fontweight='bold', color=UACJ_NEGRO)

    for i, (level, label) in enumerate(zip(f_levels, f_labels)):
        ax = axs[i // 2, i % 2]
        level_data = df[df['F_level'] == level]

        data_a = level_data[level_data['table_type'] == 'final_action']
        data_b = level_data[level_data['table_type'] == 'dates_for_filing']

        if not data_a.empty:
            ax.plot(data_a['visa_bulletin_date'], data_a['visa_wait_time'],
                    label='Fechas de Acci\u00f3n Final', color=UACJ_AZUL, linewidth=1.5)
        if not data_b.empty:
            ax.plot(data_b['visa_bulletin_date'], data_b['visa_wait_time'],
                    label='Fechas para Aplicar', color=UACJ_AMARILLO,
                    linestyle='--', linewidth=1.5)

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

    # Hide the unused 6th subplot (3x2 = 6 slots, 5 categories)
    axs[2, 1].set_visible(False)

    plt.tight_layout()
    plt.savefig(f'figures/{country}_family_visa_wait_times.png', dpi=150)
    plt.close()
