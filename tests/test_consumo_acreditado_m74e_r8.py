"""M74-E-R8 · se consume lo acreditado, no lo que haya después en la misma ruta.

Auditoría `e6c76896…`, bloqueo 1: la puerta de semillas leía la ruta para validar y otra vez para
hashear, y el evaluador la reabría después. Tres observaciones del mismo nombre permitían acreditar
A, sellar el hash de B y consumir B.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "experiments"))
sys.path.insert(0, str(RAIZ))

import aggregate_seeds as ag  # noqa: E402
import seed_coverage as sc  # noqa: E402

from tools.check_campaign_completeness import SEED_MODELS  # noqa: E402

CID, SHA, VAR = "camp_actual", "b" * 40, "camp_diff"
MODELOS = list(SEED_MODELS[VAR])


def _escribir(camp: Path, marco: pd.DataFrame, semilla: int) -> None:
    """CSV + sidecar HONESTO sobre ese CSV: lo que dejaría una promoción atómica tardía."""
    camp.mkdir(parents=True, exist_ok=True)
    csv = camp / f"global_FAD_{VAR}_s{semilla}.csv"
    marco.to_csv(csv, index=False)
    modelos = [c for c in marco.columns if c not in ("unique_id", "ds", "y")]
    side = sc.coverage_sidecar(
        marco,
        modelos,
        campaign={"campaign_id": CID, "source_git_sha": SHA},
        table="FAD",
        variant=VAR,
        seed=semilla,
        csv_sha256="sha256:" + hashlib.sha256(csv.read_bytes()).hexdigest(),
    )
    (camp / f"coverage_FAD_{VAR}_s{semilla}.json").write_text(json.dumps(side, sort_keys=True), encoding="utf-8")


def _grupo(camp: Path, nivel: pd.DataFrame) -> pd.DataFrame:
    rejilla = sc.canonical_grid(nivel, 24)
    for s in range(1, 6):
        marco = rejilla.copy()
        for k, m in enumerate(MODELOS):
            marco[m] = marco["y"] + 3.0 + k + s
        _escribir(camp, marco, s)
    return rejilla


def _series(nivel: pd.DataFrame) -> dict[str, pd.Series]:
    return {u: g.set_index("ds")["y"].astype("float64") for u, g in nivel.groupby("unique_id")}


# ═══════════════════════════════ bloqueo 1 · acreditar A y consumir A
@pytest.mark.parametrize("sustituir", [True, False], ids=["sustitucion", "control"])
def test_RED_el_agregador_evalua_lo_acreditado_aunque_la_ruta_cambie(
    tmp_path: Path, monkeypatch, panel_semillas, sustituir: bool
) -> None:
    """★ Tras la puerta se promueve un CSV de UNA fila con su sidecar coherente. El agregador tiene que
    evaluar las 25 series × 4 modelos que acreditó en cada semilla, no lo que haya después en la ruta."""
    from vp_model import artifact_receipt, dataset
    from vp_model import eval_neuralforecast as ev

    camp = tmp_path / "campaign"
    _grupo(camp, panel_semillas)
    series = _series(panel_semillas)
    monkeypatch.setattr(dataset, "load_series", lambda country, category, table: series[f"{country}/family/{category}"])
    monkeypatch.setattr(artifact_receipt, "campaign_identity", lambda *a, **k: (CID, SHA, "sha256:" + "0" * 64))
    monkeypatch.setattr(ag, "REPORTS", tmp_path)
    monkeypatch.setattr(ev, "REPORTS", tmp_path)
    puerta = sc.accredit_group

    def puerta_y_despues_sustitucion(*a, **k):
        salida = puerta(*a, **k)
        if sustituir:
            _escribir(camp, pd.read_csv(camp / f"global_FAD_{VAR}_s3.csv", parse_dates=["ds"]).head(1), 3)
        return salida

    monkeypatch.setattr(sc, "accredit_group", puerta_y_despues_sustitucion)
    vistas: dict[str, int] = {}

    def espia(df, **_k):
        vistas.update({str(k): int(v) for k, v in df.groupby("variant").size().items()})
        nulo = dict.fromkeys(("mean", "sd", "se", "lo", "hi", "min", "max"), 0.0)
        return {"per_seed": pd.Series(dtype=float), "n": 5, **nulo}

    monkeypatch.setattr(ag, "aggregate", espia)
    monkeypatch.setattr(
        sys, "argv", ["aggregate_seeds.py", "--table", "FAD", "--prefix", f"{VAR}_s", "--model", "BiTCN"]
    )
    ag.main()
    assert vistas == {f"{VAR}_s{i}": 25 * len(MODELOS) for i in range(1, 6)}


def test_RED_la_puerta_hashea_los_mismos_bytes_que_valida(tmp_path: Path, monkeypatch, panel_semillas) -> None:
    """★ La reproducción de la auditoría: justo después de parsear la semilla 3 se promueve otro CSV y su
    sidecar se ajusta al SHA de los bytes nuevos, conservando las mediciones del marco viejo."""
    rejilla = _grupo(tmp_path, panel_semillas)
    leer = pd.read_csv
    llamadas: list[int] = []

    def lectura_y_sustitucion(fuente, *a, **k):
        marco = leer(fuente, *a, **k)
        llamadas.append(1)
        if len(llamadas) == 3:
            nuevo = marco.head(1).to_csv(index=False).encode("utf-8")
            (tmp_path / f"global_FAD_{VAR}_s3.csv").write_bytes(nuevo)
            side = tmp_path / f"coverage_FAD_{VAR}_s3.json"
            d = json.loads(side.read_text(encoding="utf-8"))
            d["csv_sha256"] = "sha256:" + hashlib.sha256(nuevo).hexdigest()
            side.write_text(json.dumps(d, sort_keys=True), encoding="utf-8")
        return marco

    monkeypatch.setattr(pd, "read_csv", lectura_y_sustitucion)
    with pytest.raises(SystemExit, match="csv_sha256"):
        sc.accredit_group("FAD", VAR, tmp_path, grid=rejilla, required=MODELOS, campaign_id=CID, source_git_sha=SHA)


def test_la_puerta_devuelve_los_marcos_que_acredito(tmp_path: Path, panel_semillas) -> None:
    rejilla = _grupo(tmp_path, panel_semillas)
    marcos = sc.accredit_group(
        "FAD", VAR, tmp_path, grid=rejilla, required=MODELOS, campaign_id=CID, source_git_sha=SHA
    )
    assert sorted(marcos) == [f"{VAR}_s{i}" for i in range(1, 6)]
    assert all(len(m) == 600 and sc.seed_problems(m, rejilla, MODELOS) == [] for m in marcos.values())


def test_el_evaluador_con_marcos_no_abre_ningun_archivo(tmp_path: Path, monkeypatch, panel_semillas) -> None:
    from vp_model import dataset
    from vp_model import eval_neuralforecast as ev

    rejilla = _grupo(tmp_path, panel_semillas)
    marcos = sc.accredit_group(
        "FAD", VAR, tmp_path, grid=rejilla, required=MODELOS, campaign_id=CID, source_git_sha=SHA
    )
    series = _series(panel_semillas)
    monkeypatch.setattr(dataset, "load_series", lambda country, category, table: series[f"{country}/family/{category}"])
    monkeypatch.setattr(ev, "REPORTS", tmp_path / "no_existe")

    def prohibido(*_a, **_k):
        raise AssertionError("el evaluador abrió un archivo en vez de usar los marcos acreditados")

    monkeypatch.setattr(pd, "read_csv", prohibido)
    assert len(ev.eval_global_deep("FAD", frames=marcos)) == 5 * 25 * len(MODELOS)
