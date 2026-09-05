"""D9 — arquitectura MLOps como documento verificable (`make mlops-architecture`).

Emite DOS artefactos y nada más:

  docs/mlops_architecture.svg   diagrama canónico de UNA vista (estilo docs/schema_er.svg)
  docs/MLOPS_ARCHITECTURE.md    la página documental que lo enmarca

Ninguna cifra ni estado se teclea aquí: todo se lee de las fuentes canónicas del repo
(`reports/governance/key_facts.json`, los artefactos de gobernanza, el manifiesto del
release y `dvc.yaml`), y el guardián de consistencia vigila después ambos artefactos —así
que una añada vieja en el dibujo muere igual que en la prosa. El generador NO toca el
release vivo: escribe solo esas dos rutas.

Corre en ``ante`` desde la raíz:  ante/bin/python experiments/make_mlops_architecture.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SVG_PATH = ROOT / "docs" / "mlops_architecture.svg"
MD_PATH = ROOT / "docs" / "MLOPS_ARCHITECTURE.md"
OUTPUTS = (SVG_PATH, MD_PATH)

# Las ÚNICAS cifras de forma del dibujo: los ordinales de las cuatro capas, el primer
# horizonte (h=1) y el "0" del nombre «regla #0». No pueden coincidir con ningún hecho
# canónico (lo prueba tests/test_mlops_architecture.py), para que un valor viejo jamás
# pueda esconderse detrás de una constante.
STRUCTURAL_NUMBERS = ("0", "1", "2", "3", "4")

LAYERS = ("DATOS", "MODELADO Y EVALUACIÓN", "GOBERNANZA", "PUBLICACIÓN")

# Fuentes canónicas, en el orden en que la página las declara.
SOURCES = {
    "key_facts": "reports/governance/key_facts.json",
    "release": "reports/release/release_manifest.json",
    "ingestion": "reports/governance/ingestion_state.json",
    "promotion": "reports/governance/promotion_decision.json",
    "champion": "reports/governance/champion_manifest.json",
    "drift": "reports/governance/drift_report.json",
    "prospective": "reports/prospective/forecast_scorecard_meta.json",
    "dag": "dvc.yaml",
}


def _json(root: Path, rel: str) -> dict:
    return json.loads((root / rel).read_text(encoding="utf-8"))


def _http_code(reason: str) -> str | None:
    """Código HTTP que el estado de ingesta reporta, si lo hay (no se teclea: se lee)."""
    m = re.search(r"HTTP\s+(\d{3})", reason)
    return m.group(1) if m else None


def collect(root: Path = ROOT) -> dict:
    """Todos los datos del diagrama, leídos de sus fuentes canónicas."""
    kf = _json(root, SOURCES["key_facts"])
    rel = _json(root, SOURCES["release"])
    ing = _json(root, SOURCES["ingestion"])
    pro = _json(root, SOURCES["promotion"])
    champ = _json(root, SOURCES["champion"])
    drift = _json(root, SOURCES["drift"])
    prosp = _json(root, SOURCES["prospective"])
    dag = yaml.safe_load((root / SOURCES["dag"]).read_text(encoding="utf-8"))

    horizons = sorted(int(h) for h in prosp["by_horizon"])
    bands = sorted(int(k[3:]) for k in prosp["overall"] if k.startswith("cov") and k[3:].isdigit())
    facts: dict = {
        # panel y catálogo
        "n_obs": kf["n_obs"],
        "n_months": kf["n_months"],
        "n_obs_F": kf["n_obs_F"],
        "n_series_structural": kf["n_series_structural"],
        "n_series_with_F": kf["n_series_with_F"],
        "n_series_evaluable": kf["n_series_evaluable"],
        "n_models": kf["n_models"],
        "date_first": kf["date_first"],
        "date_last": kf["date_last"],
        "panel_hash_short": kf["panel_hash_short"],
        # evaluación prospectiva
        "prosp_n_scored": kf["prosp_n_scored"],
        "prosp_mase": kf["prosp_mase"],
        "prosp_mae_days": kf["prosp_mae_days"],
        "prosp_cov95": kf["prosp_cov95"],
        "horizon_max": horizons[-1],
        "bands": bands,
        # gobernanza
        "release_id": rel["release_id"],
        "n_artifacts": rel["n_artifacts"],
        "panel_vintage": rel["panel_vintage"],
        "expected_month": ing["expected_month"],
        "source_http_code": _http_code(str(ing.get("reason", ""))),
        "n_pairs_live": pro["n_pairs_live"],
        "promotion_decisions": {t: v["decision"] for t, v in pro["by_table"].items()},
        "policy_version": pro["policy"]["policy_version"],
        "policy_modes": list(pro["policy"]["modes_allowed"]),
        "champions": {t: list(v["models"]) for t, v in champ.items()},
        "drift_detected": drift["drift_detected"],
        "drift_data_status": drift["data"]["status"],
        "dag_stages": list(dag["stages"]),
    }
    # subconjunto numérico: lo que el guardián puede contrastar con key_facts
    facts["_numeric"] = {
        k: facts[k]
        for k in (
            "n_obs",
            "n_months",
            "n_obs_F",
            "n_series_structural",
            "n_series_with_F",
            "n_series_evaluable",
            "n_models",
            "prosp_n_scored",
            "prosp_mae_days",
            "n_artifacts",
            "n_pairs_live",
        )
    }
    return facts


def allowed_numbers(facts: dict) -> set[str]:
    """Cada número que el lector puede ver, con su forma exacta y su forma con miles."""
    out: set[str] = set(STRUCTURAL_NUMBERS)
    for value in facts["_numeric"].values():
        out |= {str(value), f"{value:,}"}
    for value in (facts["prosp_mase"], facts["prosp_cov95"]):
        out |= {str(value), f"{value:.3f}".rstrip("0")}
    out |= {str(facts["horizon_max"]), facts["policy_version"]}
    out |= {str(b) for b in facts["bands"]}
    for token in (facts["panel_vintage"], facts["expected_month"], facts["date_first"], facts["date_last"]):
        out |= set(token.split("-"))
    out |= {str(len(facts["dag_stages"])), str(len(facts["champions"]))}
    if facts["source_http_code"]:
        out.add(facts["source_http_code"])
    return out


# --- SVG -------------------------------------------------------------------------

_PALETTE = {
    "ink": "#231F20",
    "blue": "#003CA6",
    "gray": "#555559",
    "green": "#2E7D32",
    "amber": "#B26A00",
}


def _card(x: int, y: int, w: int, h: int, title: str, lines: list[str], accent: str) -> str:
    body = [
        f'  <g>\n    <rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="url(#mabg)" '
        f'stroke="{accent}" stroke-width="1.6" filter="url(#mash)"/>',
        f'    <rect x="{x}" y="{y}" width="{w}" height="26" rx="10" fill="{accent}"/>',
        f'    <rect x="{x}" y="{y + 16}" width="{w}" height="10" fill="{accent}"/>',
        f'    <text x="{x + 12}" y="{y + 18}" font-size="13" font-weight="600" fill="#ffffff">{title}</text>',
    ]
    for i, line in enumerate(lines):
        body.append(
            f'    <text x="{x + 12}" y="{y + 46 + i * 17}" font-size="12" fill="{_PALETTE["ink"]}">{line}</text>'
        )
    body.append("  </g>")
    return "\n".join(body)


def _arrow(x1: int, y1: int, x2: int, y2: int, label: str = "") -> str:
    out = (
        f'  <path d="M{x1} {y1} L{x2} {y2}" stroke="{_PALETTE["gray"]}" stroke-width="1.4" '
        f'fill="none" marker-end="url(#maarr)"/>'
    )
    if label:
        mx, my = (x1 + x2) // 2, (y1 + y2) // 2 - 6
        out += (
            f'\n  <text x="{mx}" y="{my}" font-size="10.5" fill="{_PALETTE["gray"]}" '
            f'text-anchor="middle" font-style="italic">{label}</text>'
        )
    return out


def render_svg(facts: dict) -> str:
    f = facts
    fad = "+".join(f["champions"]["FAD"])
    dff = "+".join(f["champions"]["DFF"])
    dec = " · ".join(f"{t} {d}" for t, d in sorted(f["promotion_decisions"].items()))
    code = f["source_http_code"]
    fuente = f"bloqueada ({code})" if code else "accesible"
    drift = "detectado" if f["drift_detected"] else "sin señal"
    stages = " → ".join(f["dag_stages"])
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1320 860" '
        "font-family=\"'DejaVu Sans','Helvetica Neue',Arial,sans-serif\">",
        "  <defs>",
        '    <filter id="mash" x="-12%" y="-12%" width="124%" height="140%">',
        '      <feDropShadow dx="0" dy="2.5" stdDeviation="4" flood-color="#0b1f4a" flood-opacity="0.18"/>',
        "    </filter>",
        '    <marker id="maarr" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" '
        'orient="auto-start-reverse">',
        f'      <path d="M0 0 L10 5 L0 10 z" fill="{_PALETTE["gray"]}"/>',
        "    </marker>",
        '    <linearGradient id="mabg" x1="0" y1="0" x2="0" y2="1">',
        '      <stop offset="0%" stop-color="#ffffff"/><stop offset="100%" stop-color="#f3f6fc"/>',
        "    </linearGradient>",
        "  </defs>",
        '  <rect x="0" y="0" width="1320" height="860" fill="#ffffff"/>',
        f'  <text x="40" y="46" font-size="21" font-weight="700" fill="{_PALETTE["blue"]}">'
        "VisaPredict AI — arquitectura MLOps</text>",
        f'  <text x="40" y="68" font-size="12.5" fill="{_PALETTE["gray"]}">'
        f"del boletín publicado al pronóstico servido: datos, modelado, gobernanza y publicación · "
        f"añada {f['panel_vintage']}</text>",
        # fila 1 — datos
        f'  <text x="40" y="104" font-size="12" font-weight="600" fill="{_PALETTE["gray"]}">1 · {LAYERS[0]}</text>',
        _card(
            40,
            116,
            290,
            132,
            "Ingesta y estado de la fuente",
            [
                f"travel.state.gov · {fuente}",
                f"mes esperado {f['expected_month']}",
                f"añada del panel {f['panel_vintage']}",
                "estado: ingestion_state.json",
            ],
            _PALETTE["blue"],
        ),
        _card(
            370,
            116,
            380,
            132,
            "DAG DVC reproducible",
            [
                f"{len(f['dag_stages'])} stages encadenados:",
                stages.split(" → key_facts")[0] + " →",
                "key_facts → eda_facts → fe_facts",
                "dvc.lock es un gate de CI, no un adorno",
            ],
            _PALETTE["blue"],
        ),
        _card(
            790,
            116,
            490,
            132,
            "Panel multiserie",
            [
                f"{f['n_obs']:,} filas · {f['n_months']} meses ({f['date_first']} a {f['date_last']})",
                f"{f['n_obs_F']:,} observaciones con estado F (entrenables)",
                f"{f['n_series_structural']} series catalogadas · {f['n_series_with_F']} con historia F",
                f"{f['n_series_evaluable']} evaluables tras el filtro de longitud",
            ],
            _PALETTE["blue"],
        ),
        # fila 2 — modelado
        f'  <text x="40" y="292" font-size="12" font-weight="600" fill="{_PALETTE["gray"]}">'
        "2 · MODELADO Y EVALUACIÓN</text>",
        _card(
            40,
            304,
            330,
            150,
            "Campeón y retador",
            [
                f"catálogo de {f['n_models']} modelos",
                f"campeón FAD: {fad}",
                f"campeón DFF: {dff}",
                "manifiesto: champion_manifest.json",
                "veredicto mensual, no bloqueante",
            ],
            _PALETTE["green"],
        ),
        _card(
            410,
            304,
            330,
            150,
            "Despliegue en sombra",
            [
                "ledger sombra propio e inmutable",
                "misma máquina de scoring que el campeón",
                f"{f['n_pairs_live']} pares live campeón-vs-sombra",
                "salida: prospective_head_to_head.json",
                "jamás se mezcla con el campeón",
            ],
            _PALETTE["green"],
        ),
        _card(
            780,
            304,
            500,
            150,
            "Evaluación prospectiva",
            [
                f"{f['prosp_n_scored']:,} pronósticos congelados ya puntuados",
                f"MASE {f['prosp_mase']} · MAE {f['prosp_mae_days']} días · cobertura {f['bands'][-1]}% = {f['prosp_cov95']}",
                f"horizontes h=1 a {f['horizon_max']}, bandas " + " y ".join(f"{b}%" for b in f["bands"]),
                "el pronóstico se compara con el corte realmente publicado",
                "salida: forecast_scorecard*.{csv,json}",
            ],
            _PALETTE["green"],
        ),
        # fila 3 — gobernanza
        f'  <text x="40" y="498" font-size="12" font-weight="600" fill="{_PALETTE["gray"]}">3 · GOBERNANZA</text>',
        _card(
            40,
            510,
            330,
            150,
            "Gate de promoción pre-registrado",
            [
                f"política v{f['policy_version']} · modos {', '.join(f['policy_modes'])}",
                f"decisión vigente: {dec}",
                "la muestra insuficiente NUNCA promueve",
                "la promoción real la aplica un humano",
                "salida: promotion_decision.json",
            ],
            _PALETTE["amber"],
        ),
        _card(
            410,
            510,
            330,
            150,
            "Monitor de deriva",
            [
                f"deriva: {drift} · datos {f['drift_data_status']}",
                "no es un gate: alimenta el correo",
                "3 añadas seguidas abren UN issue",
                "la re-campaña la decide un humano",
                "salida: drift_report.json",
            ],
            _PALETTE["amber"],
        ),
        _card(
            780,
            510,
            500,
            150,
            "Guardián de consistencia (regla #0)",
            [
                "key_facts.json es la fuente única de toda cifra publicada",
                "tesis, paper, web, RAG, diagramas y model card se escanean",
                "una cifra de una añada anterior tumba el CI",
                "este diagrama entra al mismo escaneo",
                "reglas: tools/consistency_rules.yml",
            ],
            _PALETTE["amber"],
        ),
        # fila 4 — publicación
        f'  <text x="40" y="704" font-size="12" font-weight="600" fill="{_PALETTE["gray"]}">4 · PUBLICACIÓN</text>',
        _card(
            40,
            716,
            620,
            108,
            "Manifiesto y procedencia del corte",
            [
                f"añada {f['panel_vintage']} · {f['n_artifacts']} artefactos con su hash",
                "identidad del release derivada de la tarjeta normalizada + el resto (D4)",
                "salidas: release_manifest.json · MODEL_CARD.md",
            ],
            _PALETTE["blue"],
        ),
        _card(
            700,
            716,
            580,
            108,
            "Cron mensual y publicación",
            [
                "un boletín nuevo dispara ingesta, modelado, EDA/FE y gobernanza",
                "release gate BLOQUEANTE y deploy solo tras CI verde del SHA exacto",
                "correo SES con fuente, deriva, advisories y tracking mensual",
            ],
            _PALETTE["blue"],
        ),
        # flechas
        _arrow(330, 182, 368, 182),
        _arrow(750, 182, 788, 182),
        _arrow(205, 248, 205, 302, "panel congelado"),
        _arrow(1030, 248, 1030, 302, "series evaluables"),
        _arrow(370, 379, 408, 379),
        _arrow(740, 379, 778, 379),
        _arrow(205, 454, 205, 508, "evidencia por pares"),
        _arrow(1030, 454, 1030, 508, "cifras vivas"),
        _arrow(205, 660, 205, 714, "decisión trazable"),
        _arrow(1030, 660, 1030, 714, "corte verificable"),
        _arrow(660, 770, 698, 770),
        f'  <text x="40" y="846" font-size="11" fill="{_PALETTE["gray"]}" font-style="italic">'
        "Generado por experiments/make_mlops_architecture.py desde las fuentes canónicas del repo; "
        "vigilado por el guardián de consistencia.</text>",
        "</svg>",
    ]
    return "\n".join(parts) + "\n"


# --- página documental -----------------------------------------------------------


def render_md(facts: dict) -> str:
    f = facts
    dec = " · ".join(f"{t}: {d}" for t, d in sorted(f["promotion_decisions"].items()))
    code = f["source_http_code"]
    fuente = f"bloqueada por el WAF, HTTP {code}" if code else "accesible"
    drift = "detectada" if f["drift_detected"] else "sin señal"
    rows = [
        (
            "Panel",
            f"{f['n_obs']:,} filas · {f['n_months']} meses · {f['n_obs_F']:,} con estado F",
            SOURCES["key_facts"],
        ),
        (
            "Series",
            f"{f['n_series_structural']} catalogadas · {f['n_series_with_F']} con historia F · "
            f"{f['n_series_evaluable']} evaluables",
            SOURCES["key_facts"],
        ),
        ("Catálogo de modelos", f"{f['n_models']} modelos", SOURCES["key_facts"]),
        (
            "Evaluación prospectiva",
            f"{f['prosp_n_scored']:,} puntuadas · MASE {f['prosp_mase']} · "
            f"MAE {f['prosp_mae_days']} días · cobertura {f['prosp_cov95']}",
            SOURCES["key_facts"],
        ),
        ("Corte publicado", f"añada {f['panel_vintage']} · {f['n_artifacts']} artefactos", SOURCES["release"]),
        ("Estado de la fuente", f"{fuente} · mes esperado {f['expected_month']}", SOURCES["ingestion"]),
        (
            "Gate de promoción",
            f"{f['n_pairs_live']} pares live · política v{f['policy_version']} · {dec}",
            SOURCES["promotion"],
        ),
        (
            "Campeones",
            " · ".join(f"{t}: {'+'.join(m)}" for t, m in sorted(f["champions"].items())),
            SOURCES["champion"],
        ),
        ("Deriva", f"{drift} · datos {f['drift_data_status']}", SOURCES["drift"]),
        ("DAG", " → ".join(f["dag_stages"]), SOURCES["dag"]),
        ("Procedencia del panel", f"hash `{f['panel_hash_short']}`", SOURCES["key_facts"]),
    ]
    table = "\n".join(f"| {name} | {value} | `{src}` |" for name, value, src in rows)
    return f"""# Arquitectura MLOps de VisaPredict AI

![Arquitectura MLOps de VisaPredict AI](mlops_architecture.svg)

Una vista: cómo un boletín publicado por el Departamento de Estado se convierte en un
pronóstico servido, y qué lo mantiene honesto por el camino. El diagrama y esta página los
genera `experiments/make_mlops_architecture.py` desde las fuentes canónicas del repositorio
(`make mlops-architecture`); ninguna cifra se teclea y el guardián de consistencia vigila
ambos artefactos, así que una añada vieja aquí tumba el CI igual que en la tesis o en el sitio.

## Las cuatro capas

**1 · Datos.** La ingesta observa la fuente y deja su veredicto en `ingestion_state.json`
(hoy {fuente}); cuando hay boletín nuevo, el **DAG de DVC** lo propaga por sus
{len(f["dag_stages"])} stages encadenados —{" → ".join(f["dag_stages"])}— y `dvc.lock` es un
gate de CI, así que la reproducibilidad no depende de la memoria de nadie. El panel resultante
es la única entrada del modelado.

**2 · Modelado y evaluación.** Un catálogo de {f["n_models"]} modelos alimenta el veredicto
**campeón-retador**; el campeón vigente vive en `champion_manifest.json`. El **despliegue en
sombra** congela la añada del retador en un ledger propio e inmutable y se puntúa con la misma
maquinaria que el campeón, nunca mezclada con él. La **evaluación prospectiva** compara cada
pronóstico congelado contra el corte realmente publicado después: es la única medida honesta
del sistema, y hoy acumula {f["prosp_n_scored"]:,} predicciones puntuadas.

**3 · Gobernanza.** El **gate de promoción** está *pre-registrado* (política
v{f["policy_version"]}, modos {", ".join(f["policy_modes"])}): se fijó con cero pares live a la
vista, la muestra insuficiente nunca promueve y la promoción real la aplica un humano. El
**monitor de deriva** no bloquea: alimenta el correo y, tras tres añadas seguidas, abre un único
issue. El **guardián de consistencia** hace cumplir la regla #0 —todo alineado siempre— usando
`key_facts.json` como fuente única de cifras para tesis, paper, sitio, RAG, model card y también
para este diagrama.

**4 · Publicación.** El **manifiesto de release** (*provenance* del corte) lo sella con su identidad y el hash de
cada artefacto, y desde D4 esa identidad se deriva de la tarjeta normalizada junto al resto de
artefactos, sin ciclo. El **cron mensual** encadena todo: ingesta, modelado, EDA/FE, gobernanza,
un release gate bloqueante y un deploy que solo ocurre tras el CI verde del SHA exacto; el correo
SES resume fuente, deriva, advisories y el tracking mensual de la corrida.

## Estado vigente y su fuente

Cada fila se lee en tiempo de generación del archivo que la nombra: si el archivo cambia y nadie
regenera, la prueba `test_committed_artifacts_match_the_generator` lo dice.

| Dato | Valor | Fuente canónica |
|---|---|---|
{table}

Corte servido en producción: `{f["release_id"]}`.

## Cómo se regenera y se verifica

```bash
make mlops-architecture     # reescribe el SVG y esta página desde las fuentes canónicas
make consistency            # el guardián valida ambos artefactos contra key_facts.json
```

Las pruebas de `tests/test_mlops_architecture.py` fijan el contrato: los artefactos commiteados
son los que el generador produce, cada número visible procede de una fuente canónica, los nueve
bloques del encargo están documentados, el generador no escribe fuera de `docs/`, y una cifra de
una añada anterior sembrada en cualquiera de los dos artefactos hace fallar al guardián.
"""


def main() -> int:
    facts = collect(ROOT)
    SVG_PATH.write_text(render_svg(facts), encoding="utf-8")
    MD_PATH.write_text(render_md(facts), encoding="utf-8")
    print(f"docs/mlops_architecture.svg + docs/MLOPS_ARCHITECTURE.md OK (añada {facts['panel_vintage']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
