"""Reconstruye y ACREDITA `holdout_forecasts_{table}.csv`, de donde cuelgan los combinadores.

El agujero que cierra, encontrado auditando el cierre de M74-E: el runbook canónico anunciaba en su
etapa 3 «holdout_forecasts frescos» y en la 4 «combinadores sobre holdouts frescos», **y nadie los
escribía**. El único escritor era este módulo y `run_rederivation.sh` no lo invocaba nunca; su
entrada principal era `persist_missing()`, que ante un CSV que ya contuviera los nombres esperados
devolvía **el archivo intacto**. Una re-derivación completa podía terminar en verde con los
ensembles, el conformal, el stacking, FFORMA, el campeón y las tablas de significancia calculados
sobre la añada **anterior**, sin una sola línea de aviso.

**Lo que hace ahora, y por qué cada pieza:**

1. **Reconstrucción completa y atómica.** Se recalcula todo el conjunto curado y se promueve con
   `os.replace`. `persist_missing` **no existe**: un artefacto que se completa por partes mezcla
   añadas por construcción, y era la única forma de que un CSV viejo sobreviviera a una campaña.
2. **Cobertura por conjunto EXACTO de claves**, derivado del **panel** —universo × pool × fechas F
   del hold-out—, nunca de lo producido. Preguntarle al sospechoso si está completo no es una
   comprobación.
3. **Fail-closed ante cualquier (serie, modelo) que falle.** Antes se anotaba `log.warning("skip
   …")` y se seguía: el CSV quedaba con menos filas y con el mismo aspecto. Si una combinación no
   se puede calcular, eso es un hallazgo de la campaña, no un detalle del artefacto.
4. **El pool sale del registro canónico** (`HOLDOUT_POOL_MODELS`), no de una constante local. La
   `CURATED` que vivía aquí era la **cuarta** lista independiente del repositorio.
5. **Recibo ligado a la campaña**, y **re-acreditación al leer**: `read_accredited()` es la única
   puerta de entrada de los consumidores.

⚠️ **El régimen no cambia:** filas **sólo** para fechas con observación F real (fix B1) y las
mismas seis columnas de siempre, para que los consumidores no vean un artefacto distinto. Lo que
cambia es que ahora está acreditado.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from vp_model import artifact_receipt as ar
from vp_model import dataset, models, walkforward
from vp_model.config import HOLDOUT, get_logger
from vp_model.model_registry import HOLDOUT_POOL_MODELS

log = get_logger("persist_forecasts")
ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"

#: Las seis columnas de siempre. Los consumidores dependen de ellas: la acreditación entra por el
#: recibo, no ensanchando el CSV.
COLUMNS = ("model", "country", "category", "date", "actual", "forecast")
KEY_FIELDS = ("model", "country", "category", "date")
SCHEMA = "holdout-forecasts-receipt/1"
#: El régimen bajo el que se calcula. Queda en el recibo y se re-exige al leer.
PROTOCOL = {
    "holdout_months": HOLDOUT,
    "f_only": True,
    "forecast_horizon": 1,
    "retrain_each_step": True,
    "block": "family",
}


class HoldoutForecastsError(ValueError):
    """El artefacto no cubre lo que el panel y el registro exigen."""


def artifact_path(table: str, reports: Path | None = None) -> Path:
    """La ruta del artefacto. ``reports`` es el ÚNICO mando: los consumidores pasan el suyo y las
    pruebas lo redirigen a un temporal, de modo que el camino acreditado se ejercita de verdad en
    vez de esquivarse."""
    return (reports or REPORTS) / "eval" / f"holdout_forecasts_{table}.csv"


def _grid(country: str, category: str, table: str):
    """La rejilla causal regular de la serie. La MISMA que usa el walk-forward."""
    return models.to_timeseries(dataset.load_series(country, category, table))


def expected_keys(
    table: str, *, block: str = "family", pool: tuple[str, ...] = HOLDOUT_POOL_MODELS
) -> set[tuple[str, str, str, str]]:
    """El conjunto de claves que DEBE existir, derivado del panel y del registro.

    Una clave por (modelo, serie, fecha) donde la fecha cae en el hold-out **y** tiene observación
    F real. Se construye sin mirar una sola fila producida.
    """
    esperado: set[tuple[str, str, str, str]] = set()
    catalogo = dataset.list_series(table=table, block=block)
    for r in catalogo.itertuples():
        fdates = set(dataset.load_series(r.country, r.category, table).index)
        ventana = _grid(r.country, r.category, table).time_index[-HOLDOUT:]
        fechas = [pd.Timestamp(d).strftime("%Y-%m-%d") for d in ventana if pd.Timestamp(d) in fdates]
        for m in pool:
            esperado |= {(m, r.country, r.category, d) for d in fechas}
    if not esperado:
        raise HoldoutForecastsError(f"{table}: el universo evaluable quedó vacío; no hay nada que acreditar")
    return esperado


def _rows(table: str, block: str, pool: tuple[str, ...]) -> list[dict]:
    """Filas largas del hold-out F-only. **Fail-closed**: nada se salta en silencio."""
    filas: list[dict] = []
    catalogo = dataset.list_series(table=table, block=block)
    for r in catalogo.itertuples():
        fdates = dataset.load_series(r.country, r.category, table).index
        for m in pool:
            try:
                ts, fc = walkforward.run_forecasts(m, r.country, r.category, table)
            except Exception as exc:
                # broad-catch: se re-lanza con contexto y ABORTA la reconstrucción. `BLE001` no
                # marca los handlers que re-lanzan (M49), así que la directiva no vale como
                # justificación y el marcador va DENTRO del handler, que es donde se busca.
                raise HoldoutForecastsError(
                    f"{table}/{r.country}/{r.category}/{m}: el walk-forward falló ({type(exc).__name__}: {exc}). "
                    "Antes esto era un `log.warning` y el CSV quedaba con menos filas y el mismo aspecto; "
                    "una combinación que no se puede calcular es un hallazgo de la campaña, no un detalle."
                ) from exc
            split = ts.time_index[-HOLDOUT]
            hold_fc = fc.split_before(split)[1]
            actual = ts.slice_intersect(hold_fc)
            fechas = actual.time_index
            fmask = fechas.isin(fdates)
            af = actual.values().flatten()[fmask]
            ff = hold_fc.slice_intersect(actual).values().flatten()[fmask]
            filas += [
                {
                    "model": m,
                    "country": r.country,
                    "category": r.category,
                    "date": pd.Timestamp(d).strftime("%Y-%m-%d"),
                    "actual": a,
                    "forecast": f,
                }
                for d, a, f in zip(fechas[fmask], af, ff, strict=True)
            ]
        log.info("hold-out forecasts: %s/%s listo", r.country, r.category)
    return filas


def audit(filas: list[dict], esperado: set[tuple]) -> dict:
    """Conjunto observado contra esperado. Un conteo es una coincidencia; un conjunto, una identidad."""
    if not filas:
        raise HoldoutForecastsError("el artefacto está vacío: ninguna fila que acreditar")
    claves = [tuple(str(f[k]) for k in KEY_FIELDS) for f in filas]
    vistos: set[tuple] = set()
    duplicados: set[tuple] = set()
    for k in claves:
        (duplicados if k in vistos else vistos).add(k)
    faltan, sobran = esperado - vistos, vistos - esperado
    problemas = []
    if duplicados:
        problemas.append(f"{len(duplicados)} DUPLICADA(s), p. ej. {sorted(duplicados)[:2]}")
    if faltan:
        problemas.append(f"{len(faltan)} AUSENTE(s), p. ej. {sorted(faltan)[:2]}")
    if sobran:
        problemas.append(f"{len(sobran)} NO ESPERADA(s), p. ej. {sorted(sobran)[:2]}")
    if problemas:
        raise HoldoutForecastsError("cobertura rota: " + " · ".join(problemas))
    modelos = sorted({f["model"] for f in filas})
    return {"n_rows": len(filas), "n_keys": len(vistos), "models": modelos, "n_models": len(modelos)}


def rebuild(
    table: str,
    *,
    campaign_id: str,
    code_sha: str,
    panel_sha256: str,
    reports: Path | None = None,
    block: str = "family",
    pool: tuple[str, ...] = HOLDOUT_POOL_MODELS,
) -> Path:
    """Reconstrucción COMPLETA y atómica, acreditada. Nunca parcial, nunca incremental."""
    esperado = expected_keys(table, block=block, pool=pool)
    filas = _rows(table, block, pool)
    censo = audit(filas, esperado)
    destino = artifact_path(table, reports)
    marco = pd.DataFrame(filas, columns=list(COLUMNS)).sort_values(list(KEY_FIELDS), kind="stable")
    ar.replace_atomic(destino, lambda t: marco.to_csv(t, index=False))
    recibo = ar.seal(
        destino,
        schema=SCHEMA,
        campaign_id=campaign_id,
        code_sha=code_sha,
        panel_sha256=panel_sha256,
        protocol={**PROTOCOL, "block": block},
        coverage=censo,
        expected_keys=esperado,
        extra={"pool": list(pool), "table": table},
    )
    log.info("%s: %d filas · %d modelos · recibo %s", table, censo["n_rows"], censo["n_models"], recibo.name)
    return destino


def read_accredited(
    table: str,
    *,
    reports: Path | None = None,
    campaign_id: str | None = None,
    code_sha: str | None = None,
    panel_sha256: str | None = None,
    block: str = "family",
) -> pd.DataFrame:
    """★ La ÚNICA puerta de entrada de los consumidores. Re-acredita en cada lectura.

    Los ocho `pd.read_csv` repartidos por combinadores, campeón y tablas de significancia leían el
    archivo que hubiera. Ahora pasan por aquí y un artefacto de otra campaña, sin recibo o tocado
    después del sellado **no se consume**.
    """
    reports = reports or REPORTS
    if campaign_id is None or code_sha is None or panel_sha256 is None:
        campaign_id, code_sha, panel_sha256 = ar.campaign_identity(reports)
    destino = artifact_path(table, reports)
    ar.verify(
        destino,
        schema=SCHEMA,
        campaign_id=campaign_id,
        code_sha=code_sha,
        panel_sha256=panel_sha256,
        protocol={**PROTOCOL, "block": block},
    )
    return pd.read_csv(destino, parse_dates=["date"])


def main(argv: list[str] | None = None) -> int:
    """Reconstruye AMBAS tablas. Sin identidad de campaña, aborta: un artefacto sin procedencia
    es peor que ninguno, porque tiene el mismo aspecto que uno bueno."""
    import sys

    try:
        campaign_id, code_sha, panel = ar.campaign_identity(REPORTS)
    except ar.ReceiptError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    for table in ("FAD", "DFF"):
        destino = rebuild(table, campaign_id=campaign_id, code_sha=code_sha, panel_sha256=panel)
        print(f"✓ {destino.relative_to(ROOT)} reconstruido y acreditado")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
