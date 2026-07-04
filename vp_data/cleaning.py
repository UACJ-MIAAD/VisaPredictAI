"""Central registry of the data-cleaning policy + machine-readable build ledger.

Two things live here (FE/cleaning plan, epics AA1/AA2):

* ``CLEANING_DECISIONS`` — the single enumeration of every deliberate cleaning
  decision, with its owning module and the failure it prevents. The prose twin
  is ``docs/CLEANING.md``; downstream consumers (fe_facts, the FE report, the
  web #fe section) derive their "decisiones magistrales" narrative FROM THIS
  TUPLE so the story can never drift from the code that implements it.
* ``write_ledger`` — persists per-build cleaning counts (rows collapsed,
  coerced, flagged) that previously lived only in ephemeral ``logger.warning``
  lines. Deterministic on purpose (no wall clock: the vintage month is the
  version key) so the DVC panel stage stays byte-reproducible.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

LEDGER_PATH = Path("reports/governance/cleaning_ledger.json")

# Every deliberate cleaning decision: what, where it is enforced, why.
# rationale in Spanish because it feeds the (Spanish) report/web verbatim.
CLEANING_DECISIONS: tuple[dict[str, str], ...] = (
    {
        "id": "status_regime",
        "title": "Régimen C/F/U/UNK como anotación, F como único objetivo",
        "module": "vp_data/visa_common.py:classify_status",
        "rationale": (
            "Aplanar C→fecha y U→NaN destruía el régimen administrativo. La columna "
            "status lo preserva; solo las celdas F (fecha específica) son objetivo "
            "predictivo (formulación v5.1) y la evaluación enmascara todo lo demás (B1)."
        ),
    },
    {
        "id": "unk_sentinel",
        "title": "Centinela UNK (nunca el string NA)",
        "module": "vp_data/visa_common.py:classify_status",
        "rationale": (
            "El literal 'NA' colisiona con la coerción por defecto de pandas.read_csv "
            "(lo lee como NaN) y borraba la anotación. UNK distingue 'sin dato' de "
            "'Unavailable' y sobrevive a cualquier consumidor downstream."
        ),
    },
    {
        "id": "century_pivot",
        "title": "Pivote de siglo con guardia de época",
        "module": "vp_data/visa_common.py:string_to_datetime",
        "rationale": (
            "Las celdas publican años de 2 dígitos ('01MAY16'); strptime pivota "
            "69..99→19xx. Una fecha F anterior a t0=1975 haría days_since_base "
            "negativo: build_panel aborta (underflow) y el CHECK days_is_datediff "
            "del almacén re-verifica la aritmética completa."
        ),
    },
    {
        "id": "footnote_tolerance",
        "title": "Tolerancia a erratas y notas de la fuente",
        "module": "vp_data/visa_common.py:string_to_datetime/classify_status",
        "rationale": (
            "20 años de boletines traen footnotes (C*/U*), espacios y erratas ('4rd'). "
            "El parser normaliza sin descartar el mes; lo imparseable queda UNK con su "
            "raw_value intacto (nada se corrige en silencio: la celda cruda se conserva)."
        ),
    },
    {
        "id": "dedup_regime_preference",
        "title": "Deduplicación por preferencia de régimen F>C>U>UNK",
        "module": "pipeline/build_panel.py:main",
        "rationale": (
            "En transiciones de etiqueta (p.ej. EB-5 'Unreserved' 2022) una categoría "
            "canónica aparece dos veces el mismo mes. 'first' era una moneda al aire que "
            "podía tirar una observación F entrenable; se prefiere F y se ABORTA si dos "
            "F del mismo mes discrepan (conflicto de fuente que resuelve un humano)."
        ),
    },
    {
        "id": "date_failfast",
        "title": "Fechas imparseables abortan en la causa",
        "module": "pipeline/build_panel.py:main",
        "rationale": (
            "Una fecha F coercionada a NaT violaría days_iff_F lejos de su causa (en el "
            "CHECK del almacén); un bulletin_date NaT viajaría hasta el merge de "
            "dim_date. Ambos abortan en build_panel con las filas culpables (AA3)."
        ),
    },
    {
        "id": "domain_validation",
        "title": "Dominios de categoría validados al leer",
        "module": "pipeline/build_panel.py:load_employment/load_family",
        "rationale": (
            "keep_default_na=False protege el centinela UNK pero desactiva la coerción "
            "NA de todo el frame; un literal extraño en F_level/EB_level pasaría como "
            "string. El dominio se valida explícitamente tras cada read_csv (AA4)."
        ),
    },
    {
        "id": "gap_policy_training",
        "title": "Huecos: interpolar ≤3 meses; largos NaN; relleno solo para entrenar",
        "module": "vp_model/preprocess.py:to_regular_monthly · vp_model/models.py:to_timeseries",
        "rationale": (
            "Los huecos son meses C/U (MNAR: la ausencia es señal). Corridas ≤3 meses se "
            "interpolan linealmente; las largas quedan NaN (todo-o-nada por corrida, sin "
            "rampas parciales). to_timeseries rellena los NaN residuales SOLO para dar "
            "continuidad al entrenamiento — jamás son objetivo: la evaluación puntúa "
            "únicamente fechas F reales (máscara B1, fuente única metrics._aligned)."
        ),
    },
    {
        "id": "eda_kalman",
        "title": "Caracterización EDA imputa con Kalman, nunca rampas sin tope",
        "module": "vp_model/features.py:_clean · vp_model/missingness.py:kalman_impute",
        "rationale": (
            "STL/espectro/catch22 exigen series completas. Los huecos largos se imputan "
            "con suavizado de Kalman (espacio de estados, imputeTS::na_kalman), no con "
            "interpolación lineal multi-año ni extrapolación de bordes: una rampa "
            "inventada fabrica tendencia y contamina Hurst/changepoints/entropía (AB1)."
        ),
    },
    {
        "id": "stationarity_on_raw_F",
        "title": "Pruebas formales sobre las F crudas (con caveat de espaciado)",
        "module": "experiments/build_eda_facts.py:_formal_tests",
        "rationale": (
            "ADF/KPSS/DF-GLS corren sobre las observaciones F sin imputar: imputar antes "
            "de una prueba de raíz unitaria sesga hacia 'integrada'. Costo aceptado y "
            "documentado: en series con huecos el índice queda comprimido y la "
            "estructura de rezagos asume espaciado regular (AB3)."
        ),
    },
    {
        "id": "outliers_as_signal",
        "title": "Retrogresiones = señal; outliers se cuentan, jamás se recortan",
        "module": "vp_model/features.py:count_outliers · pipeline/mega_audit.py:d9_jumps",
        "rationale": (
            "Las retrogresiones y saltos >8 años son eventos administrativos reales que "
            "el modelo debe tolerar (la tesis lo argumenta). Ningún paso winsoriza ni "
            "elimina valores extremos del dato; solo se CUENTAN con estadísticos "
            "robustos (z-STL, Hampel) y las figuras anotan lo que quede fuera de rango "
            "en vez de recortarlo en silencio (AC1/AC2)."
        ),
    },
    {
        "id": "schema_contract",
        "title": "El contrato se re-verifica declarativamente en el almacén",
        "module": "schema.sql:days_iff_F/pdate_iff_F/days_is_datediff/rank_iff_F",
        "rationale": (
            "Las invariantes de limpieza no viven solo en Python: los CHECK/PK/FK del "
            "esquema estrella rechazan en la carga cualquier fila que las viole, con el "
            "nombre exacto de la invariante rota."
        ),
    },
)


def write_ledger(entry: dict[str, Any]) -> Path:
    """Write the per-build cleaning ledger (git-versioned, deterministic)."""
    payload = {
        "_source": "pipeline/build_panel.py via vp_data.cleaning — NO editar a mano",
        "_policy": "docs/CLEANING.md",
        **entry,
    }
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_PATH.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n")
    return LEDGER_PATH
