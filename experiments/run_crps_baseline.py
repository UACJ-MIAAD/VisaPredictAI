"""CRPS de los modelos DISTRIBUCIONALES clásicos sobre el hold-out (deuda-2, 2-jul-2026).

Regenera ``reports/eval/crps_fad.csv`` (model, country, category, crps) — el insumo del panel
CRPS de ``make_result_figures.fig_coverage_crps`` y de la prosa del entregable ("SARIMA
48 / ARIMA 49 / DeepAR 174 días"). El CSV original fue una corrida one-off sin escritor
versionado; este script la hace reproducible y, tras B1, la puntúa SOLO sobre fechas F
reales (``walkforward.crps_holdout`` ya enmascara).

★ M74-F · C6: el CSV lleva recibo (``champion.CRPS_BASE_RECEIPT_SCHEMA``) cuando la corrida es la COMPLETA
(``champion.CRPS_BASE_PROTOCOLO``) y tiene identidad que prestar: una campaña ``running`` o, con
``--complement``, la complementaria ``baseline_crps`` en curso. Una serie que falla queda como celda vacía y
declarada en el recibo con su motivo. Sin identidad, el CSV se escribe sin firmar y su consumidor
(``champion.baseline_crps_provenance``) lo trata como heredado.

Corre en ``ante``:  ante/bin/python experiments/run_crps_baseline.py [--table FAD]
                    ante/bin/python experiments/run_crps_baseline.py --complement
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vp_model import champion, dataset, walkforward  # noqa: E402
from vp_model.config import PROBABILISTIC, get_logger  # noqa: E402

log = get_logger("crps_baseline")
REPORTS = ROOT / "reports"


def _score(model: str, country: str, category: str, table: str, num_samples: int) -> float:
    """CRPS de un modelo en una serie (el productor caro; las pruebas lo doblan)."""
    return walkforward.crps_holdout(model, country, category, table, num_samples=num_samples)


def _protocol(table: str, block: str) -> dict:
    proto = champion.CRPS_BASE_PROTOCOLO
    return {**proto, "table": table, "block": block}


def run(table: str, block: str, *, complement: bool = False) -> pd.DataFrame:
    num_samples = int(champion.CRPS_BASE_PROTOCOLO["num_samples"])
    cat = dataset.list_series(table=table, block=block)
    rows, skipped = [], []
    for r in cat.itertuples():
        for m in sorted(PROBABILISTIC):
            try:
                c = _score(m, r.country, r.category, table, num_samples)
            except Exception as e:  # noqa: BLE001 — B6: fallo visible, serie no desaparece muda
                log.warning("FAIL %s %s/%s: %s: %s", m, r.country, r.category, type(e).__name__, str(e)[:60])
                c = float("nan")
                skipped.append({"model": m, "country": r.country, "category": r.category,
                                "reason": f"{type(e).__name__}: {e}"[:300]})  # fmt: skip
            rows.append({"model": m, "country": r.country, "category": r.category, "crps": c})
            log.info("%s/%s %-8s CRPS=%.1f", r.country, r.category, m, c)
    out = REPORTS / "eval" / f"crps_{table.lower()}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    df.to_csv(out, index=False)
    log.info("escrito -> %s · medias: %s", out, df.groupby("model").crps.mean().round(1).to_dict())
    _seal(out, table, block, skipped, complement=complement)
    return df


def _seal(out: Path, table: str, block: str, skipped: list[dict], *, complement: bool = False) -> None:
    """Recibo del CSV (M74-F · C6): sólo la corrida completa con identidad que prestar se firma."""
    from vp_model import artifact_receipt as ar

    if _protocol(table, block) != champion.CRPS_BASE_PROTOCOLO:
        log.warning("%s/%s no es el protocolo acreditable: el CSV queda SIN recibo", table, block)
        return
    if complement:
        from vp_model import complement as cx

        try:  # el código o la DuckDB pudieron cambiar mientras se calculaba: sin precondiciones no hay firma
            ident = cx.identidad_crps(REPORTS.parent, duckdb_consumida=Path(dataset.DB_PATH), proposito="baseline_crps")
        except cx.ComplementoError as exc:
            raise SystemExit(f"✗ el CRPS de referencia no se firma: {exc}") from exc
        cid, sha, panel = ident["campaign_id"], ident["source_git_sha"], ident["panel_sha256"]
    else:
        try:
            cid, sha, panel = ar.campaign_identity(REPORTS, code_root=REPORTS.parent)
        except ar.ReceiptError as exc:
            log.warning("fuera de campaña, el CSV queda SIN recibo (heredado para F4): %s", exc)
            return
    recibo = ar.seal(
        out,
        schema=champion.CRPS_BASE_RECEIPT_SCHEMA,
        campaign_id=cid,
        code_sha=sha,
        panel_sha256=panel,
        protocol=champion.CRPS_BASE_PROTOCOLO,
        coverage={"table": table, "skipped": sorted(skipped, key=lambda o: tuple(o.values()))},
    )
    print(f"   recibo {recibo.name} · {cid}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--table", default=champion.CRPS_BASE_PROTOCOLO["table"], choices=("FAD", "DFF"))
    ap.add_argument("--block", default=champion.CRPS_BASE_PROTOCOLO["block"])
    ap.add_argument("--complement", action="store_true", help="C6 inside the running baseline_crps complement")
    args = ap.parse_args(argv)
    if args.complement:
        from vp_model import complement as cx

        if _protocol(args.table, args.block) != champion.CRPS_BASE_PROTOCOLO:
            raise SystemExit(f"✗ C6 sólo acredita la corrida completa {champion.CRPS_BASE_PROTOCOLO}")
        try:
            cx.precondiciones_calculo(REPORTS.parent, proposito="baseline_crps", duckdb_consumida=Path(dataset.DB_PATH))
        except cx.ComplementoError as exc:
            raise SystemExit(f"✗ el CRPS de referencia no puede calcularse aquí: {exc}") from exc
    elif (REPORTS / "campaign" / "complement.json").exists():
        raise SystemExit("✗ este árbol tiene una transacción complementaria: corre con --complement")
    run(args.table, args.block, complement=args.complement)


if __name__ == "__main__":
    main()
