"""G7 · las tres puertas que se cierran ANTES de propagar (adenda 1 de la ruta de cierre, 12-oct-2026).

1. `artifact_receipt.read_running_transaction` aceptaba cualquier `campaign.json` con `status == running`: una
   transacción de juguete (cuatro claves), de otro `schema_version` o con identidad mal formada prestaba su
   identidad a los artefactos. Ahora exige el esquema de la máquina de estados (ADR 0003).
2. F5 (`make_result_figures.fig_backtest_grid`) y la figura hero sombreaban un hold-out FIJO (2024-08→2026-07):
   con el panel que termina en septiembre de 2026 el hold-out real es 2024-10→2026-09. La ventana se deriva del
   protocolo (`config.HOLDOUT` meses que terminan en el último boletín del panel) y los pronósticos dibujados
   tienen que cubrir EXACTAMENTE esa ventana; si son de otra añada, la figura se niega.
3. `sync_all.sh --publish` sin cambios en los punteros terminaba en 0 («sin cambios que commitear») sin publicar
   ni consumir el permiso: un verde que no publicó nada.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import matplotlib as mpl
import pandas as pd
import pytest

from tools import campaign_state as cs
from vp_model import artifact_receipt as ar

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ / "experiments") not in sys.path:  # los scripts de figuras se importan como módulos
    sys.path.insert(0, str(RAIZ / "experiments"))
HEX40 = "a" * 40


# ═══════════════════════════════ puerta 1 · la transacción se lee con su esquema
def _sellada(tmp_path: Path) -> Path:
    """Una transacción `running` escrita por el ESCRITOR REAL (`seal_running`), no a mano."""
    reports = tmp_path / "reports"
    cs.seal_running(
        reports / "campaign" / "campaign.json",
        campaign_id="rederiv_g7_t1",
        source_git_sha=HEX40,
        git_dirty=False,
        panel_sha256="sha256:" + "b" * 64,
        started_at="2026-10-14T12:00:00+00:00",
        input_seal_sha256="c" * 64,
    )
    return reports


def test_puerta1_control_la_transaccion_del_escritor_real_se_lee(tmp_path: Path) -> None:
    estado = ar.read_running_transaction(_sellada(tmp_path))
    assert estado["campaign_id"] == "rederiv_g7_t1" and estado["schema_version"] == cs.SCHEMA_VERSION


def _juguete(doc: dict) -> dict:
    return {k: doc[k] for k in ("status", "campaign_id", "source_git_sha", "panel_sha256")}


@pytest.mark.parametrize(
    "mutar",
    [
        _juguete,
        lambda d: {**d, "schema_version": 2},
        lambda d: {**d, "clave_ajena": 1},
        lambda d: {**d, "source_git_sha": "n/d"},
        lambda d: {**d, "panel_sha256": "n/d"},
        lambda d: {**d, "git_dirty": "false"},
        lambda d: {k: v for k, v in d.items() if k != "input_seal_sha256"},
        lambda d: {**d, "completed_at": "2026-10-14T13:00:00+00:00"},
    ],
    ids=[
        "juguete",
        "otro_esquema",
        "clave_ajena",
        "sha_mal",
        "panel_nd",
        "dirty_texto",
        "sin_sello",
        "running_terminado",
    ],
)
def test_puerta1_una_transaccion_fuera_de_esquema_no_presta_identidad(tmp_path: Path, mutar) -> None:
    reports = _sellada(tmp_path)
    txn = reports / "campaign" / "campaign.json"
    txn.write_text(json.dumps(mutar(json.loads(txn.read_text()))))
    with pytest.raises(ar.ReceiptError, match="esquema"):
        ar.read_running_transaction(reports)


def test_puerta1_el_validador_tiene_una_sola_autoridad() -> None:
    """`vp_model` no importa `tools` (ADR-0001): el esquema vive en `vp_model` y la máquina lo reexporta."""
    from vp_model import campaign_schema

    assert cs.validate_schema is campaign_schema.validate_schema
    assert cs.SCHEMA_VERSION == campaign_schema.SCHEMA_VERSION


# ═══════════════════════════════ puerta 2 · el hold-out de las figuras se deriva del protocolo
NUEVA = pd.date_range("2024-10-01", "2026-09-01", freq="MS")  # 24 meses que terminan en el último boletín
VIEJA = pd.date_range("2024-08-01", "2026-07-01", freq="MS")  # la añada de los literales
FIN_PANEL = pd.Timestamp("2026-09-01")


def _deep_pi(meses: pd.DatetimeIndex, series: list[str]) -> pd.DataFrame:
    filas = []
    for uid in series:
        for i, ds in enumerate(meses):
            v = 20000.0 + 10 * i
            fila = {"unique_id": uid, "ds": ds, "y": v, "BiTCN": v + 1}
            for lvl in (50, 80, 95):
                fila[f"BiTCN-lo-{lvl}"] = v - lvl
                fila[f"BiTCN-hi-{lvl}"] = v + lvl
            filas.append(fila)
    return pd.DataFrame(filas)


def _serie(*a, **k) -> pd.Series:
    idx = pd.date_range("2019-01-01", "2026-09-01", freq="MS")
    return pd.Series(range(19000, 19000 + len(idx)), index=idx, dtype="float64")


@pytest.fixture
def figuras(tmp_path: Path, monkeypatch):
    """Los dos scripts de figuras con datos de prueba y `_emit`/`savefig` dobles que cierran sin escribir."""
    import matplotlib.pyplot as plt

    from vp_model import dataset

    with mpl.rc_context():
        mrf = importlib.import_module("make_result_figures")
        hero = importlib.import_module("make_hero_figures")
    (tmp_path / "reports" / "eval").mkdir(parents=True)
    monkeypatch.setattr(mrf, "REP", tmp_path / "reports")
    monkeypatch.setattr(hero, "ROOT", tmp_path)
    monkeypatch.setattr(dataset, "load_series", _serie)
    # la fuente del fin del panel en las pruebas: el panel de juguete termina en septiembre de 2026. En la línea
    # base el módulo no existe y las figuras corren con sus literales: así la RED muestra lo que dibujan hoy.
    try:
        import vp_model.holdout_window as hw
    except ImportError:
        pass
    else:
        monkeypatch.setattr(hw, "panel_end", lambda *a, **k: FIN_PANEL)
    emitidas: list[str] = []

    def emitir(fig, nombre: str) -> None:  # como el real: registra y CIERRA la figura, sin escribir
        emitidas.append(nombre)
        plt.close(fig)

    def guardar(self, *a, **k) -> None:  # la hero guarda con savefig: registra, no escribe
        emitidas.append("hero")

    monkeypatch.setattr(mrf, "_emit", emitir)
    monkeypatch.setattr(plt.Figure, "savefig", guardar)
    franjas: list[tuple] = []
    lineas: list[pd.Timestamp] = []
    axvspan, axvline = plt.Axes.axvspan, plt.Axes.axvline

    def span(ax, a, b, *x, **k):
        franjas.append((pd.Timestamp(a), pd.Timestamp(b)))
        return axvspan(ax, a, b, *x, **k)

    def line(ax, x, *a, **k):
        lineas.append(pd.Timestamp(x))
        return axvline(ax, x, *a, **k)

    monkeypatch.setattr(plt.Axes, "axvspan", span)
    monkeypatch.setattr(plt.Axes, "axvline", line)
    yield {"mrf": mrf, "hero": hero, "eval": tmp_path / "reports" / "eval", "emitidas": emitidas,
           "franjas": franjas, "lineas": lineas}  # fmt: skip
    plt.close("all")


def _escribir_deep_pi(f: dict, meses: pd.DatetimeIndex) -> None:
    series = [f"{a}/family/{c}" for a, _ in f["mrf"].AREAS for c in f["mrf"].CATS]
    for t in ("FAD", "DFF"):
        _deep_pi(meses, series).to_csv(f["eval"] / f"deep_pi_{t}.csv", index=False)


def test_puerta2_f5_sombrea_el_hold_out_que_dicta_el_protocolo(figuras) -> None:
    _escribir_deep_pi(figuras, NUEVA)
    figuras["mrf"].fig_backtest_grid("FAD")
    assert figuras["franjas"] and set(figuras["franjas"]) == {(NUEVA[0], NUEVA[-1])}
    assert figuras["emitidas"] == ["results_backtest_grid_FAD"] or len(figuras["emitidas"]) == 1


def test_puerta2_la_hero_separa_historia_y_pronostico_donde_empieza_el_hold_out(figuras) -> None:
    _escribir_deep_pi(figuras, NUEVA)
    figuras["hero"].fig_hero()
    assert figuras["lineas"] == [NUEVA[0]]


@pytest.mark.parametrize("figura", ["f5", "hero"])
def test_puerta2_pronosticos_de_otra_anada_no_se_dibujan(figuras, figura: str) -> None:
    _escribir_deep_pi(figuras, VIEJA)
    with pytest.raises((SystemExit, ValueError), match="añada|ventana"):
        figuras["mrf"].fig_backtest_grid("FAD") if figura == "f5" else figuras["hero"].fig_hero()
    assert figuras["emitidas"] == []


# ═══════════════════════════════ puerta 3 · `sync_all --publish` no termina en verde sin publicar
def _git(raiz: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}  # fmt: skip
    hooks = raiz.parent / "sin_hooks"
    hooks.mkdir(exist_ok=True)
    return subprocess.run(["git", "-c", f"core.hooksPath={hooks}", "-c", "commit.gpgsign=false", *args], cwd=raiz,
                          env=env, capture_output=True, text=True, check=True).stdout  # fmt: skip


@pytest.fixture
def repo_sync(tmp_path: Path) -> Path:
    """Un repo con el `sync_all.sh` REAL; intérprete y DVC falsos registran lo que se les pide y salen en 0."""
    raiz = tmp_path / "repo"
    (raiz / "experiments").mkdir(parents=True)
    shutil.copy(RAIZ / "experiments" / "sync_all.sh", raiz / "experiments" / "sync_all.sh")
    for rel in ("ante_nf/bin/python", "ante/bin/dvc"):
        falso = raiz / rel
        falso.parent.mkdir(parents=True, exist_ok=True)
        falso.write_text(
            '#!/bin/bash\nprintf "%s %s\\n" "$(basename "$0")" "$*" >> "$REGISTRO"\n'
            'case " $* " in *" ${FALLA_EN:-__nunca__} "*) exit 3;; esac\necho fijo\n'
        )
        falso.chmod(0o755)
    for rel in ("models.dvc", "mlflow.db.dvc", ".gitignore"):
        (raiz / rel).write_text("puntero\n")
    (raiz / "reports" / "campaign").mkdir(parents=True)
    _git(raiz, "init", "-q", "-b", "main")
    _git(raiz, "add", "-A")
    _git(raiz, "commit", "-qm", "base")
    # un remoto real (bare): un push que falla por falta de remoto escondería el verde falso
    remoto = tmp_path / "remoto.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remoto)], check=True)
    _git(raiz, "remote", "add", "origin", str(remoto))
    _git(raiz, "push", "-q", "-u", "origin", "main")
    return raiz


def _sync(raiz: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "REGISTRO": str(raiz.parent / "registro.txt"), "SYNC_PUBLISH": "0",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}  # fmt: skip
    env.pop("VP_DVC", None)
    return subprocess.run(["bash", "experiments/sync_all.sh", *args], cwd=raiz, env=env, capture_output=True,
                          text=True, timeout=60)  # fmt: skip


def test_puerta3_publicar_no_prepara_ni_empuja_git(repo_sync: Path) -> None:
    """G7 · desbloqueo: publicar consume una release YA integrada; no sincroniza, no re-hashea, no commitea ni empuja.

    Antes `--publish` commiteaba y empujaba git y sólo después `txn publish` comprobaba el árbol aprobado.
    """
    remoto = repo_sync.parent / "remoto.git"
    antes = (_git(repo_sync, "rev-parse", "HEAD"), _git(remoto, "rev-parse", "main"))
    fin = _sync(repo_sync, "--publish")
    assert fin.returncode == 0, fin.stdout + fin.stderr
    registro = (repo_sync.parent / "registro.txt").read_text().splitlines()
    assert not [ll for ll in registro if ll.startswith("dvc ") or "sync_mlflow" in ll], registro
    pasos = [next(p for p in ("--assert-publishable", "guard", "publish-release") if p in ll) for ll in registro]
    assert pasos == ["--assert-publishable", "guard", "publish-release"], registro
    assert "--repo . --main-ref origin/main" in registro[-1], registro[-1]
    assert (_git(repo_sync, "rev-parse", "HEAD"), _git(remoto, "rev-parse", "main")) == antes


@pytest.mark.parametrize("falla", ["guard", "--assert-publishable"])
def test_puerta3_si_la_puerta_no_autoriza_no_hay_ningun_efecto(repo_sync: Path, monkeypatch, falla: str) -> None:
    monkeypatch.setenv("FALLA_EN", falla)
    fin = _sync(repo_sync, "--publish")
    assert fin.returncode in (7, 8), fin.stdout + fin.stderr
    registro = (repo_sync.parent / "registro.txt").read_text()
    assert "publish-release" not in registro and "dvc " not in registro, registro


def test_puerta3_control_sin_publish_y_sin_cambios_sigue_en_verde(repo_sync: Path) -> None:
    fin = _sync(repo_sync)
    assert fin.returncode == 0 and "sin cambios que commitear" in fin.stdout, fin.stdout + fin.stderr


def test_puerta2_sin_pronosticos_no_hay_figura(figuras) -> None:
    """Un `deep_pi` sin filas no es «una ventana sin datos»: es que no hay nada que dibujar."""
    _deep_pi(NUEVA, []).reindex(columns=["unique_id", "ds", "y", "BiTCN"]).to_csv(
        figuras["eval"] / "deep_pi_FAD.csv", index=False
    )
    with pytest.raises(ValueError, match="no hay pronósticos"):
        figuras["mrf"].fig_backtest_grid("FAD")
    assert figuras["emitidas"] == []


# ═══════════════════════════════ auditoría de las puertas (ronda 2): bordes
@pytest.mark.parametrize(
    "ids",
    [[None, None], ["mexico/family/F1", None], ["", ""], ["mexico/family/F1", "  "]],
    ids=["todos_nulos", "uno_nulo", "vacios", "uno_en_blanco"],
)
def test_borde_una_fila_sin_serie_identificada_se_rechaza(ids, monkeypatch) -> None:
    """`groupby` descarta las claves nulas: si todas lo son, no se valida ningún grupo y la ventana «pasaba»."""
    import vp_model.holdout_window as hw

    monkeypatch.setattr(hw, "panel_end", lambda *a, **k: FIN_PANEL)
    d = pd.concat([_deep_pi(NUEVA, ["x"]).assign(unique_id=uid) for uid in ids], ignore_index=True)
    with pytest.raises(ValueError, match="serie identificada"):
        hw.window_of(d)


def test_borde_f5_no_dibuja_filas_sin_serie(figuras) -> None:
    _deep_pi(NUEVA, [""]).to_csv(figuras["eval"] / "deep_pi_FAD.csv", index=False)  # se relee como NaN
    with pytest.raises(ValueError, match="serie identificada"):
        figuras["mrf"].fig_backtest_grid("FAD")
    assert figuras["emitidas"] == []


@pytest.mark.parametrize("args", [["--publish"], []], ids=["publish", "local"])
def test_borde_un_staging_ajeno_se_rechaza_sin_tocarlo(repo_sync: Path, args: list[str]) -> None:
    """La condición miraba TODO el índice: un archivo ajeno ya preparado se commiteaba y publicaba con los punteros."""
    (repo_sync / "ajeno.txt").write_text("de otro trabajo\n")
    _git(repo_sync, "add", "ajeno.txt")
    head = _git(repo_sync, "rev-parse", "HEAD")
    fin = _sync(repo_sync, *args)
    assert fin.returncode != 0 and "ajeno" in fin.stderr, fin.stdout + fin.stderr
    assert _git(repo_sync, "rev-parse", "HEAD") == head, "nada se commitea"
    assert _git(repo_sync, "diff", "--cached", "--name-only").split() == ["ajeno.txt"], "el staging ajeno se preserva"
    registro = repo_sync.parent / "registro.txt"
    assert not registro.exists() or registro.read_text() == "", "se rechaza ANTES de tocar MLflow, DVC o el índice"


# ═══════════════════════════════ mark_published (decisión B del autor): sin API pública de escritura
from tests.test_campaign_hardening_m74b import _manifiesto, _validada  # noqa: E402
from tools import campaign_txn as txn  # noqa: E402

#: G7 · B5a: estas puertas son ORTOGONALES al vínculo del recibo /3 (reconciliación + candidato); ese vínculo se
#: prueba de punta a punta en `test_g7_publicacion_reconciliada`. Aquí se sustituye sólo esa verificación.
pytestmark = pytest.mark.usefixtures("vinculo_fuera_de_esta_prueba")


def test_b_la_maquina_no_expone_una_escritura_de_published() -> None:
    publicas = [n for n in vars(cs) if not n.startswith("_") and callable(getattr(cs, n)) and "publish" in n.lower()]
    assert publicas == [], "la transición a `published` sólo se alcanza por la entrada gobernada"


def test_b_control_la_entrada_gobernada_reacredita_y_publica(tmp_path: Path, campana_sellada) -> None:
    p = _validada(tmp_path, campana_sellada)
    obj = txn.publish(p, manifest=_manifiesto(tmp_path), release_sha="b" * 40)
    assert obj["status"] == "published" and (cs.read(p) or {})["release_sha"] == "b" * 40
    assert not txn.publishable(p, manifest=_manifiesto(tmp_path))[0], "el permiso se consume"


def test_b_sin_puerta_no_se_escribe(tmp_path: Path, campana_sellada) -> None:
    p = _validada(tmp_path, campana_sellada)
    antes = p.read_bytes()
    with pytest.raises(txn.PublicationBlocked):
        txn.publish(p, manifest=tmp_path / "no_existe.json", release_sha="b" * 40)
    assert p.read_bytes() == antes


def test_b_si_la_transaccion_cambia_tras_la_puerta_no_se_publica(tmp_path: Path, campana_sellada, monkeypatch) -> None:
    """La escritura es CAS sobre la revisión que la puerta acreditó: un cambio entre ambas la invalida."""
    p = _validada(tmp_path, campana_sellada)
    puerta = txn._puerta

    def puerta_y_cambio(*a, **k):
        veredicto = puerta(*a, **k)
        cs.mark_failed(p, failed_stage="concurrente", failed_at=txn.now_rfc3339(), reason="cambio tras la puerta")
        return veredicto

    monkeypatch.setattr(txn, "_puerta", puerta_y_cambio)
    with pytest.raises(ValueError):
        txn.publish(p, manifest=_manifiesto(tmp_path), release_sha="b" * 40)
    assert (cs.read(p) or {})["status"] == "failed"


def test_b_un_validated_reescrito_tras_la_puerta_no_se_publica(tmp_path: Path, campana_sellada, monkeypatch) -> None:
    """Sigue en `validated` pero ya no es el que la puerta acreditó (otra revisión, otro revisor): sin el CAS sobre
    la revisión, `published` se escribiría sobre un estado que nadie acreditó."""
    p = _validada(tmp_path, campana_sellada)
    puerta = txn._puerta

    def puerta_y_reescritura(*a, **k):
        veredicto = puerta(*a, **k)
        doc = cs.read(p) or {}
        cs.atomic_write(p, {**doc, "reviewed_by": "otra persona", "revision": int(doc["revision"]) + 1})
        return veredicto

    monkeypatch.setattr(txn, "_puerta", puerta_y_reescritura)
    with pytest.raises(ValueError, match="revision stale"):
        txn.publish(p, manifest=_manifiesto(tmp_path), release_sha="b" * 40)
    assert (cs.read(p) or {})["status"] == "validated"
