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
6. **★ R20 · exclusiones ACREDITADAS, nunca silenciosas.** La segunda campaña real
   (`rederiv_9b9489f_20260916T151013`, 17-sep-2026) murió aquí a las 13 h: SARIMA no converge en
   India F2B (`LinAlgError: LU decomposition error`) y el punto 3 abortaba la reconstrucción entera,
   mientras la etapa 1 de la MISMA campaña había dejado esa (modelo, serie) sin `hold_mase` y había
   seguido: dos etapas con dos definiciones de «completo». Ahora una (modelo, serie) que no se puede
   calcular se admite **sólo si la etapa 1 de esta campaña la dejó sin métricas** en su pool
   (`campaign_pool_{table}_{block}.csv`, misma `run_id`); queda en el recibo con su error, el conjunto
   esperado se acredita SIN sus claves, y el lector vuelve a derivar los fallos de la etapa 1 y exige
   que coincidan EXACTAMENTE con lo excluido. Un fallo que la etapa 1 no tuvo sigue abortando.

⚠️ **El régimen no cambia:** filas **sólo** para fechas con observación F real (fix B1) y las
mismas seis columnas de siempre, para que los consumidores no vean un artefacto distinto. Lo que
cambia es que ahora está acreditado.
"""

from __future__ import annotations

import math
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


#: Memoria del conjunto esperado por (tabla, bloque, pool, panel). Recalcularlo en CADA lectura
#: sería recorrer el catálogo entero por consumidor; la clave incluye el hash del panel, así que un
#: panel distinto no reutiliza nada. No es un respaldo: si no está, se deriva.
_MEMORIA_ESPERADO: dict[tuple, set[tuple[str, str, str, str]]] = {}


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


def pool_artifact_path(table: str, block: str = "family", reports: Path | None = None) -> Path:
    """El pool de la etapa 1 (`run_comparison`), la única autoridad sobre qué (modelo, serie) no se calculó."""
    return (reports or REPORTS) / "campaign" / f"campaign_pool_{table}_{block}.csv"


def stage1_failures(
    table: str,
    *,
    campaign_id: str,
    block: str = "family",
    pool: tuple[str, ...] = HOLDOUT_POOL_MODELS,
    reports: Path | None = None,
) -> set[tuple[str, str, str]]:
    """★ R20 · (modelo, país, categoría) del pool × catálogo que la etapa 1 de ESTA campaña dejó sin
    `hold_mase` finito. Se exige exactamente una fila por combinación con la `run_id` de la campaña:
    un pool de otra corrida, incompleto o duplicado no justifica nada."""
    ruta = pool_artifact_path(table, block, reports)
    if not ruta.is_file():
        raise HoldoutForecastsError(f"sin pool de la etapa 1 en {ruta}: no hay veredicto que justifique exclusiones")
    marco = pd.read_csv(ruta, dtype={"run_id": str, "model": str, "country": str, "category": str})
    marco = marco[marco["run_id"] == campaign_id]
    fallos: set[tuple[str, str, str]] = set()
    for r in dataset.list_series(table=table, block=block).itertuples():
        for m in pool:
            fila = marco[(marco["model"] == m) & (marco["country"] == r.country) & (marco["category"] == r.category)]
            if len(fila) != 1:
                raise HoldoutForecastsError(
                    f"{table}/{r.country}/{r.category}/{m}: el pool de la etapa 1 de {campaign_id} tiene "
                    f"{len(fila)} fila(s) para la combinación y se exige exactamente una"
                )
            valor = fila["hold_mase"].iloc[0]
            if pd.isna(valor) or not math.isfinite(float(valor)):
                fallos.add((m, r.country, r.category))
    return fallos


def excluded_keys(esperado: set[tuple[str, str, str, str]], excluidas: list[dict]) -> set[tuple[str, str, str, str]]:
    """Las claves del conjunto esperado que caen en las (modelo, serie) excluidas."""
    combos = {(str(e["model"]), str(e["country"]), str(e["category"])) for e in excluidas}
    return {k for k in esperado if (k[0], k[1], k[2]) in combos}


def _rows(
    table: str, block: str, pool: tuple[str, ...], *, campaign_id: str, reports: Path | None = None
) -> tuple[list[dict], list[dict]]:
    """Filas largas del hold-out F-only y las (modelo, serie) EXCLUIDAS. **Fail-closed**: nada se salta
    en silencio; una exclusión sólo se admite si la etapa 1 de esta campaña ya falló en la misma
    combinación (R20), y queda registrada con su error."""
    filas: list[dict] = []
    excluidas: list[dict] = []
    fallos_etapa1: set[tuple[str, str, str]] | None = None
    catalogo = dataset.list_series(table=table, block=block)
    for r in catalogo.itertuples():
        fdates = dataset.load_series(r.country, r.category, table).index
        for m in pool:
            try:
                ts, fc = walkforward.run_forecasts(m, r.country, r.category, table)
            except Exception as exc:
                # broad-catch: se re-lanza con contexto y ABORTA la reconstrucción salvo que la etapa 1
                # de ESTA campaña ya haya fallado aquí. `BLE001` no marca los handlers que re-lanzan
                # (M49): el marcador va DENTRO del handler, que es donde se busca.
                if fallos_etapa1 is None:
                    fallos_etapa1 = stage1_failures(
                        table, campaign_id=campaign_id, block=block, pool=pool, reports=reports
                    )
                if (m, r.country, r.category) not in fallos_etapa1:
                    raise HoldoutForecastsError(
                        f"{table}/{r.country}/{r.category}/{m}: el walk-forward falló ({type(exc).__name__}: {exc}) "
                        "y la etapa 1 de esta campaña SÍ lo calculó: no es una exclusión acreditable, es un fallo. "
                        "Una combinación que no se puede calcular es un hallazgo de la campaña, no un detalle."
                    ) from exc
                error = f"{type(exc).__name__}: {str(exc)[:120]}"
                excluidas.append({"model": m, "country": r.country, "category": r.category, "error": error})
                log.warning(
                    "hold-out forecasts: %s/%s/%s EXCLUIDO como en la etapa 1 (%s)", r.country, r.category, m, error
                )
                continue
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
    return filas, sorted(excluidas, key=lambda e: (e["model"], e["country"], e["category"]))


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
    # ★ R2 · los VALORES también se auditan. `audit()` aceptaba `actual=NaN` y `forecast=inf`:
    #   comprobaba que las filas correctas estuvieran y no que dijeran algo. Un NaN se propaga a la
    #   media del ensemble y un inf revienta el MASE, y ambos llegaban con el recibo en regla.
    malos = [
        f"{f['model']}/{f['country']}/{f['category']}@{f['date']}"
        for f in filas
        if not (
            isinstance(f["actual"], (int, float))
            and isinstance(f["forecast"], (int, float))
            and math.isfinite(float(f["actual"]))
            and math.isfinite(float(f["forecast"]))
        )
    ]
    if malos:
        raise HoldoutForecastsError(
            f"{len(malos)} fila(s) con actual/forecast no finito, p. ej. {malos[:3]}. "
            "Un NaN se propaga a la media del ensemble y un infinito revienta el MASE"
        )
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
    filas, excluidas = _rows(table, block, pool, campaign_id=campaign_id, reports=reports)
    efectivo = esperado - excluded_keys(esperado, excluidas)  # R20: lo excluido no se espera, y queda en el recibo
    censo = audit(filas, efectivo)
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
        expected_keys=efectivo,
        extra={"pool": list(pool), "table": table, "excluded": excluidas},
    )
    log.info("%s: %d filas · %d modelos · recibo %s", table, censo["n_rows"], censo["n_models"], recibo.name)
    return destino


#: Claves EXACTAS del recibo. Cerrado en ambos sentidos: ni de más ni de menos.
RECEIPT_KEYS = frozenset(
    {
        "schema",
        "campaign_id",
        "code_sha",
        "panel_sha256",
        "protocol",
        "coverage",
        "artifact_sha256",
        "expected_keys_sha256",
        "n_expected_keys",
        "pool",
        "table",
        "excluded",
    }
)
#: Claves EXACTAS de cada exclusión declarada en el recibo.
EXCLUSION_KEYS = frozenset({"model", "country", "category", "error"})


def _validar_exclusiones(
    excluidas: object, esperado: set[tuple[str, str, str, str]], *, table: str, campaign_id: str, reports: Path
) -> set[tuple[str, str, str, str]]:
    """★ R20 · las exclusiones del recibo se RE-DERIVAN de la etapa 1: el conjunto declarado tiene que
    ser exactamente el de las (modelo, serie) del pool × catálogo sin `hold_mase` en el pool de esta
    campaña. Ni una de más (una exclusión inventada) ni una de menos (un fallo de la etapa 1 que aquí
    «sí se calculó»). Devuelve las claves que dejan de esperarse."""
    if not isinstance(excluidas, list) or any(not isinstance(e, dict) or set(e) != EXCLUSION_KEYS for e in excluidas):
        raise HoldoutForecastsError(f"recibo con `excluded` malformado: se exige una lista de {sorted(EXCLUSION_KEYS)}")
    combos = [(e["model"], e["country"], e["category"]) for e in excluidas]
    if any(not all(isinstance(e[k], str) and e[k] for k in EXCLUSION_KEYS) for e in excluidas) or len(
        set(combos)
    ) != len(combos):
        raise HoldoutForecastsError(
            "recibo con `excluded` malformado: campos vacíos, no textuales o combinaciones repetidas"
        )
    universo = {(k[0], k[1], k[2]) for k in esperado}
    if not set(combos) <= universo:
        raise HoldoutForecastsError(
            f"el recibo excluye combinaciones fuera del universo: {sorted(set(combos) - universo)[:3]}"
        )
    if not combos:
        return set()
    fallos = stage1_failures(table, campaign_id=campaign_id, block=str(PROTOCOL["block"]), reports=reports)
    if set(combos) != fallos:
        raise HoldoutForecastsError(
            f"las exclusiones del recibo no son los fallos de la etapa 1 de {campaign_id}: "
            f"sobran {sorted(set(combos) - fallos)[:3]}, faltan {sorted(fallos - set(combos))[:3]}"
        )
    return excluded_keys(esperado, excluidas)


#: Claves EXACTAS del censo de cobertura dentro del recibo.
COVERAGE_KEYS = frozenset({"n_rows", "n_keys", "models", "n_models"})


def read_accredited(table: str, *, reports: Path | None = None) -> pd.DataFrame:
    """★ La ÚNICA puerta de entrada de los consumidores. Re-acredita en cada lectura.

    Los NUEVE `pd.read_csv` repartidos por combinadores, campeón y tablas de significancia leían el
    archivo que hubiera. (⚠️ R1 dijo «ocho»: recontado sobre `41cb225` son **nueve** — siete con el
    literal en el argumento y dos por una variable con la ruta, en `run_ensembles.py` y
    `ensemble.py`.) Ahora pasan por aquí, y un artefacto de otra campaña, sin recibo o tocado
    después del sellado **no se consume**.

    ★ **M74-E-R3 · aquí NO se pasa identidad.** R2 admitía `campaign_id`, `code_sha` y
    `panel_sha256` por parámetro, y con los tres puestos **nunca se llamaba a `campaign_identity`**:
    un artefacto con las 5 400 claves reales, valores finitos y un recibo coherente con identidades
    **inventadas** pasaba entero **sin que existiera `campaign.json`**. Los nueve consumidores no
    usaban el atajo —el camino canónico estaba protegido—, pero la frontera pública no.

    ⚠️ Y el atajo existía **porque mis propias pruebas dependían de él**. Es la forma exacta que
    ya conocía este repositorio: un bypass sobrevive porque algo lo usa, y casi siempre es una
    prueba. Ahora las pruebas montan una transacción coherente, como hace la campaña.

    Tampoco se admiten `pool`, `block` ni `code_root`: cada uno era una palanca para encoger lo que
    se comprueba. Todo sale de las constantes canónicas y del registro.
    """
    reports = reports or REPORTS
    campaign_id, code_sha, panel_sha256 = ar.campaign_identity(reports, code_root=ROOT)
    block, pool = str(PROTOCOL["block"]), HOLDOUT_POOL_MODELS
    destino = artifact_path(table, reports)
    acta = ar.verify(
        destino,
        schema=SCHEMA,
        campaign_id=campaign_id,
        code_sha=code_sha,
        panel_sha256=panel_sha256,
        protocol={**PROTOCOL, "block": block},
    )
    # ── la CABECERA exacta: un CSV con columnas de más, de menos o en otro orden no es este
    #    artefacto aunque su hash cuadre con un recibo que también se haya escrito para él.
    with open(destino, encoding="utf-8") as fh:
        cabecera = fh.readline().strip()
    if cabecera != ",".join(COLUMNS):
        raise HoldoutForecastsError(f"cabecera {cabecera!r}; el esquema exacto es {','.join(COLUMNS)!r}")

    marco = pd.read_csv(destino, parse_dates=["date"])
    filas = [{**r, "date": pd.Timestamp(r["date"]).strftime("%Y-%m-%d")} for r in marco.to_dict(orient="records")]
    # ★ R2 · EL punto. Hasta aquí el lector verificaba procedencia y hash y luego **daba por buena
    #   la cobertura que declaraba el escritor**: un CSV de una sola fila con un recibo que decía
    #   5 400 pasaba entero. El conjunto esperado se RECALCULA del panel y se vuelve a auditar.
    clave = (table, block, pool, panel_sha256)
    esperado = _MEMORIA_ESPERADO.get(clave)
    if esperado is None:
        esperado = expected_keys(table, block=block, pool=pool)
        _MEMORIA_ESPERADO[clave] = esperado
    # ── el esquema del recibo, ANTES de leer nada de él
    #    ★ R3: R2 sólo comparaba `n_rows` y `n_keys`. Un recibo puede mentir en cualquier otro
    #    campo con la misma facilidad, y un esquema abierto admite campos que nadie mira.
    if set(acta) != RECEIPT_KEYS:
        sobran, faltan = sorted(set(acta) - RECEIPT_KEYS), sorted(RECEIPT_KEYS - set(acta))
        raise HoldoutForecastsError(f"recibo con esquema abierto — sobran {sobran}, faltan {faltan}")
    # ★ R20 · lo excluido se re-deriva de la etapa 1 y se resta del universo; el resto se exige entero.
    esperado = esperado - _validar_exclusiones(
        acta["excluded"], esperado, table=table, campaign_id=campaign_id, reports=reports
    )
    censo = audit(filas, esperado)

    # ── y el recibo tiene que estar de acuerdo ENTERO con lo que acabamos de medir
    if acta.get("table") != table:
        raise HoldoutForecastsError(f"el recibo es de la tabla {acta.get('table')!r} y se pide {table!r}")
    if tuple(acta.get("pool") or ()) != pool:
        raise HoldoutForecastsError(
            f"el recibo declara el pool {acta.get('pool')!r} y el registro canónico fija {list(pool)}"
        )
    sello = ar.expected_keys_sha256(esperado)
    if acta.get("expected_keys_sha256") != sello:
        raise HoldoutForecastsError(
            f"el recibo fija un conjunto esperado {str(acta.get('expected_keys_sha256'))[:12]}… y el "
            f"panel de hoy exige {sello[:12]}…: se acreditó contra otro universo"
        )
    if acta.get("n_expected_keys") != len(esperado):
        raise HoldoutForecastsError(
            f"el recibo dice esperar {acta.get('n_expected_keys')!r} claves y el panel exige {len(esperado)}"
        )
    declarada = acta.get("coverage") or {}
    if set(declarada) != COVERAGE_KEYS:
        raise HoldoutForecastsError(
            f"censo de cobertura con claves {sorted(declarada)}, se exigen {sorted(COVERAGE_KEYS)}"
        )
    for campo in sorted(COVERAGE_KEYS):
        declarado, medido = declarada.get(campo), censo[campo]
        if (list(declarado) if isinstance(declarado, list) else declarado) != medido:
            raise HoldoutForecastsError(f"el recibo declara {campo}={declarado!r} y el artefacto mide {medido!r}")
    return marco


def main(argv: list[str] | None = None) -> int:
    """Reconstruye AMBAS tablas. Sin identidad de campaña, aborta: un artefacto sin procedencia
    es peor que ninguno, porque tiene el mismo aspecto que uno bueno."""
    import sys

    try:
        campaign_id, code_sha, panel = ar.campaign_identity(REPORTS, code_root=ROOT)
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
