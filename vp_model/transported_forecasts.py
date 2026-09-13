"""El artefacto largo de los pronósticos TRANSPORTADOS, con su recibo cerrado (§8.7).

`ets` y `theta` son `AutoETS`/`AutoTheta`: rechazan `historical_forecasts(retrain=False)`, así que
**nadie puede recalcularlos** después del walk-forward. Su pronóstico se transporta desde donde ya
se calcula. Este módulo escribe ese transporte y lo acredita.

⚠️ **El recibo exige el CONJUNTO EXACTO de claves, no un conteo.** «24 filas por grupo» pasa con
una serie sustituida por otra, con un duplicado que compensa un faltante o con meses equivocados.
Lo que se compara es el conjunto de claves **esperado** —derivado del universo vigente y del
registro canónico— contra el **observado**. Un conteo es una coincidencia; un conjunto es una
identidad.

⚠️ **El consumidor sólo acepta el artefacto de SU campaña**: sin respaldo a archivos anteriores y
sin caer a `retrain=True`. La imposibilidad técnica de persistir ETS/Theta no los vuelve opcionales.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

#: Clave única cerrada de cada fila (§8.7.2).
KEY_FIELDS = ("campaign_id", "table", "country", "category", "model", "origin", "target", "h")
ROW_FIELDS = (*KEY_FIELDS, "y_true", "y_pred", "observed", "mase_scale", "code_sha", "panel_sha256")
RECEIPT_SCHEMA = "transported-forecasts-receipt/1"
#: El régimen del walk-forward oficial, fijado en el recibo (§8.7.3).
PROTOCOL = {
    "forecast_horizon": 1,
    "stride": 1,
    "last_points_only": True,
    "retrain_each_step": True,
    "targets_per_group": 24,
}
ARTIFACT = Path("reports") / "eval" / "transported_forecasts.csv"


class TransportError(ValueError):
    """El transporte no cubre lo que el registro y el universo exigen, o no es de esta campaña."""


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def expected_keys(
    *, campaign_id: str, universe: list[tuple[str, str, str]], models: tuple[str, ...], targets: dict
) -> set[tuple]:
    """El conjunto de claves que DEBE existir. Derivado del universo y del registro, no de las filas.

    ``universe`` son las combinaciones elegibles ``(table, country, category)`` y ``targets`` mapea
    cada combinación a sus objetivos del hold-out. Construir esto a partir de lo producido sería
    preguntarle al sospechoso si el conjunto está completo.
    """
    esperado = set()
    for table, country, category in universe:
        objetivos = targets[(table, country, category)]
        if len(objetivos) != PROTOCOL["targets_per_group"]:
            raise TransportError(
                f"{table}/{country}/{category}: el hold-out declara {len(objetivos)} objetivos y el "
                f"protocolo exige {PROTOCOL['targets_per_group']}"
            )
        for model in models:
            for origin, target in objetivos:
                esperado.add(
                    (campaign_id, table, country, category, model, origin, target, PROTOCOL["forecast_horizon"])
                )
    return esperado


def panel_sha256_of(root: Path) -> str:
    """El `panel_sha256` que la transacción selló. Fail-closed: sin campaña, no hay identidad."""
    txn = Path(root) / "reports" / "campaign" / "campaign.json"
    if not txn.is_file():
        raise TransportError(f"no hay transacción de campaña en {txn}: el transporte no puede acreditarse")
    estado = json.loads(txn.read_text(encoding="utf-8"))
    valor = estado.get("panel_sha256")
    if not isinstance(valor, str) or not valor:
        raise TransportError("la transacción no sella un panel_sha256")
    return valor


def _key_of(fila: dict[str, Any]) -> tuple:
    return tuple(fila[k] for k in KEY_FIELDS)


def audit_rows(filas: list[dict[str, Any]], esperado: set[tuple]) -> dict[str, Any]:
    """Compara conjunto observado contra esperado. Fail-closed ante faltantes, sobras o duplicados."""
    if not filas:
        raise TransportError("el transporte está vacío: ninguna fila que acreditar")
    faltan_campos = {c for f in filas for c in ROW_FIELDS if c not in f}
    if faltan_campos:
        raise TransportError(f"filas sin los campos obligatorios: {sorted(faltan_campos)}")

    claves = [_key_of(f) for f in filas]
    vistos: set[tuple] = set()
    duplicados: set[tuple] = set()
    for k in claves:
        (duplicados if k in vistos else vistos).add(k)
    faltantes = esperado - vistos
    sobrantes = vistos - esperado
    problemas = []
    if duplicados:
        problemas.append(f"{len(duplicados)} clave(s) DUPLICADA(s), p. ej. {sorted(duplicados)[:2]}")
    if faltantes:
        problemas.append(f"{len(faltantes)} clave(s) AUSENTE(s), p. ej. {sorted(faltantes)[:2]}")
    if sobrantes:
        problemas.append(f"{len(sobrantes)} clave(s) NO ESPERADA(s), p. ej. {sorted(sobrantes)[:2]}")
    if problemas:
        raise TransportError("cobertura del transporte rota: " + " · ".join(problemas))

    # la escala del MASE viaja en cada fila y tiene que ser utilizable (§8.7.3)
    malas = [f for f in filas if not isinstance(f["mase_scale"], (int, float)) or f["mase_scale"] <= 0]
    if malas:
        raise TransportError(
            f"{len(malas)} fila(s) con mase_scale no finita o no positiva: una escala degenerada "
            "convierte el MASE en otra métrica"
        )
    # ── invariantes FILA POR FILA, no sobre una muestra
    import math as _math

    from pandas import Timestamp as _TS

    coherencia: list[str] = []
    for f in filas:
        clave = f"{f['table']}/{f['country']}/{f['category']}/{f['model']}@{f['target']}"
        # (1) `observed` y `y_true` cuentan la MISMA historia. Un observado sin real es una métrica
        #     que no puede calcularse; un no observado CON real es un mes que entró por la puerta
        #     de atrás en una serie que el protocolo declara no evaluable.
        yt = f["y_true"]
        if f["observed"]:
            if yt is None or not isinstance(yt, (int, float)) or not _math.isfinite(float(yt)):
                coherencia.append(f"{clave}: observed=true con y_true={yt!r}")
        elif yt is not None and yt != "":
            coherencia.append(f"{clave}: observed=false con y_true={yt!r}")
        # (2) el horizonte y el origen, en TODAS las filas
        if f["h"] != PROTOCOL["forecast_horizon"]:
            coherencia.append(f"{clave}: h={f['h']!r} y el protocolo fija {PROTOCOL['forecast_horizon']}")
        esperado_origen = (_TS(f["target"]).to_period("M") - 1).to_timestamp().strftime("%Y-%m-%d")
        if str(f["origin"]) != esperado_origen:
            coherencia.append(f"{clave}: origin={f['origin']!r}; por periodo mensual sería {esperado_origen!r}")
    if coherencia:
        raise TransportError(f"{len(coherencia)} fila(s) incoherentes, p. ej.: " + " · ".join(coherencia[:3]))

    observadas = sum(1 for f in filas if f["observed"])
    return {
        "n_rows": len(filas),
        "n_keys": len(vistos),
        "n_observed": observadas,
        "n_unobserved": len(filas) - observadas,
    }


def write(
    filas: list[dict[str, Any]], esperado: set[tuple], *, root: Path, campaign_id: str, code_sha: str, panel_sha256: str
) -> Path:
    """Escribe el artefacto por **staging** y sella su recibo AL FINAL. Acredita antes de promover."""
    import csv

    censo = audit_rows(filas, esperado)
    destino = root / ARTIFACT
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(ROW_FIELDS))
        w.writeheader()
        for f in sorted(filas, key=_key_of):
            w.writerow({k: f[k] for k in ROW_FIELDS})
    os.replace(tmp, destino)

    recibo = destino.with_suffix(".receipt.json")
    datos = {
        "schema": RECEIPT_SCHEMA,
        "campaign_id": campaign_id,
        "code_sha": code_sha,
        "panel_sha256": panel_sha256,
        "models": sorted({f["model"] for f in filas}),
        "protocol": PROTOCOL,
        "coverage": censo,
        "expected_keys_sha256": hashlib.sha256(
            "\n".join("|".join(map(str, k)) for k in sorted(esperado)).encode()
        ).hexdigest(),
        "artifact_sha256": _sha256(destino),
    }
    tmp_r = recibo.with_suffix(".json.tmp")
    tmp_r.write_text(json.dumps(datos, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp_r, recibo)
    return recibo


def load_and_accredit(
    *, root: Path, campaign_id: str, code_sha: str, panel_sha256: str, esperado: set[tuple]
) -> list[dict[str, Any]]:
    """Para el EXPORTADOR. Sólo el artefacto de SU campaña; sin respaldo ni `retrain=True`."""
    import csv

    destino = root / ARTIFACT
    recibo = destino.with_suffix(".receipt.json")
    if not destino.is_file() or not recibo.is_file():
        raise TransportError(
            f"falta el transporte o su recibo ({destino.name}/{recibo.name}). ETS y Theta no son "
            "opcionales: sin transporte acreditado, no hay exportación"
        )
    acta = json.loads(recibo.read_text(encoding="utf-8"))
    if acta.get("schema") != RECEIPT_SCHEMA:
        raise TransportError(f"recibo con esquema {acta.get('schema')!r}")
    for clave, esperado_v in (("campaign_id", campaign_id), ("code_sha", code_sha), ("panel_sha256", panel_sha256)):
        if acta.get(clave) != esperado_v:
            raise TransportError(
                f"el transporte dice {clave}={acta.get(clave)!r} y la campaña activa {esperado_v!r}: otra corrida"
            )
    real = _sha256(destino)
    if acta.get("artifact_sha256") != real:
        raise TransportError(f"{destino.name} cambió desde el sellado (sha {real[:12]}…)")
    if acta.get("protocol") != PROTOCOL:
        raise TransportError(f"el recibo declara otro régimen de walk-forward: {acta.get('protocol')}")

    with open(destino, newline="", encoding="utf-8") as fh:
        filas = [
            {
                **r,
                "h": int(r["h"]),
                "y_pred": float(r["y_pred"]),
                "y_true": None if r["y_true"] in ("", "None") else float(r["y_true"]),
                "observed": r["observed"] in ("True", "true", "1"),
                "mase_scale": float(r["mase_scale"]),
            }
            for r in csv.DictReader(fh)
        ]
    audit_rows(filas, esperado)  # ★ se re-acredita al LEER, no sólo al escribir
    return filas
