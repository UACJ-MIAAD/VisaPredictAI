"""M74-E-R14 · lo que la auditoría ciega read-only sobre `9c5b908` encontró en el CAMINO REAL.

Tres bloqueos, los tres reproducidos por conducta y ninguno visible desde las pruebas que había:

1. **El sello no se acreditaba bajo el entorno que el propio runbook crea.** `run_rederivation.sh`
   exporta `PYTHONPATH="$PWD"` antes del preflight; el censo del intérprete heredaba ese entorno y
   `importlib.metadata` descubría el `visapredictai.egg-info` de la raíz como distribución sin
   `direct_url`: `reproduces_lock:false` en los dos intérpretes y `--assert-sealed` con exit 13
   antes de abrir la transacción. El preflight doble era byte-idéntico… con el mismo veredicto falso.
2. **El staging de finalistas exportaba variables que nadie leía.** `save_finalists.sh` fijaba
   `VP_MODELS_DIR`/`VP_MODELS_MANIFEST`; los productores escribían en `models/`; el promotor moría
   en `[2b/4]` buscando un manifiesto en el staging. Y aunque lo hubiera, `promote()` movía las
   tablas sin promover el manifiesto.
3. **`sync_all.sh` invocaba un `dvc` a secas** del PATH del operador: en el worktree de ejecución
   no hay `ante/bin/dvc` y ninguna puerta lo miraba.

Las pruebas son herméticas salvo las que miden los venvs reales, que se saltan sin ellos.
"""

from __future__ import annotations

import copy
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "experiments"))

from tools import campaign_manifest as cm  # noqa: E402
from tools import campaign_preflight as pf  # noqa: E402
from tools import check_env_matches_lock as el  # noqa: E402
from tools.check_dvc_lock_fresh import resolve_dvc  # noqa: E402

VENVS_REALES = all((RAIZ / v / "bin" / "python").is_file() for v, _ in el.INTERPRETER_LOCKS)
RUNBOOK = RAIZ / "experiments" / "run_rederivation.sh"


def _ejecutable(p: Path, texto: str) -> Path:
    p.write_text(texto, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return p


# ═══════════════════════════════ 1 · el censo del intérprete ignora el PYTHONPATH heredado
def _interprete_contaminante(tmp_path: Path) -> Path:
    """Un `bin/python` que exporta un PYTHONPATH con una distribución que SÓLO existe ahí y ejecuta
    el intérprete real. Si el censo hereda el entorno, la ve; si corre aislado, no."""
    dist = tmp_path / "contaminado" / "contaminante-1.0.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: contaminante\nVersion: 1.0\n", encoding="utf-8")
    venv = tmp_path / "ante_nf"
    (venv / "bin").mkdir(parents=True)
    _ejecutable(
        venv / "bin" / "python",
        f'#!/bin/sh\nexport PYTHONPATH="{tmp_path / "contaminado"}"\nexec "{sys.executable}" "$@"\n',
    )
    return venv


def test_RED_el_censo_del_interprete_ignora_el_PYTHONPATH_heredado(tmp_path: Path) -> None:
    """Contra `9c5b908` la distribución fantasma entra al censo y bloquea el intérprete."""
    venv = _interprete_contaminante(tmp_path)
    lock = tmp_path / "locks" / "x.txt"
    lock.parent.mkdir()
    lock.write_text(f"pip=={importlib.metadata.version('pip')}\n", encoding="utf-8")
    r = el.comparar(venv, lock)
    assert "contaminante" not in r["extra"], "una distribución que sólo existe en PYTHONPATH entró al censo"
    assert "contaminante" not in r["undeclared"]


@pytest.mark.skipif(not VENVS_REALES, reason="sin ante/ y ante_nf/ en disco (runner de CI)")
def test_RED_el_sello_reproduce_el_lock_bajo_el_PYTHONPATH_del_runbook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, campana_sellada
) -> None:
    """Los venvs REALES, medidos con y sin el `PYTHONPATH="$PWD"` que `run_rederivation.sh` exporta
    antes del preflight: el veredicto no puede depender de él. Contra `9c5b908`, con la variable
    puesta aparecía `visapredictai` sin `direct_url` y `reproduces_lock` pasaba a `false`.

    En el worktree de EJECUCIÓN (entornos exactos al lock) se exige además que el sello real se
    acredite; en un worktree de desarrollo con entornos divergentes esa mitad no aplica."""
    monkeypatch.setenv("VP_DEEP_ACCEL", "cpu")
    if resolve_dvc(RAIZ, os.environ) is None:  # el worktree de ejecución no trae ante/bin/dvc
        monkeypatch.setenv("VP_DVC", str(_ejecutable(tmp_path / "dvc", "#!/bin/sh\necho 3.67.1\n")))
    monkeypatch.delenv("PYTHONPATH", raising=False)
    limpio = el.auditar(RAIZ)
    monkeypatch.setenv("PYTHONPATH", str(RAIZ))
    assert el.auditar(RAIZ) == limpio, "el PYTHONPATH heredado cambió el censo o la acreditación del intérprete"
    if all(v["reproduces_lock"] for v in limpio.values()):
        sello = pf.seal()
        m = campana_sellada(tmp_path, head=sello["git"]["head"], dirty=sello["git"]["dirty"], sello=sello)
        assert cm.seal_problems(m) == []


# ═══════════════════════════════ 2 · el DVC gobernado se sella y se exige
def test_el_sello_registra_el_dvc_gobernado_y_el_esquema_lo_exige(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, campana_sellada
) -> None:
    sello = campana_sellada.sello()
    assert set(sello["environment"]["dvc"]) == {"executable", "version"}
    sin = copy.deepcopy(sello)
    sin["environment"].pop("dvc")
    assert cm.seal_problems(campana_sellada(tmp_path, sello=sin)), "un sello sin el DVC se acreditó"
    raro = copy.deepcopy(sello)
    raro["environment"]["dvc"]["version"] = "desconocida"
    assert cm.seal_problems(campana_sellada(tmp_path, sello=raro))
    # y se MIDE: la versión sale del binario resuelto, y sin binario no hay sello
    falso = _ejecutable(tmp_path / "dvc", "#!/bin/sh\necho 9.9.9\n")
    monkeypatch.setenv("VP_DVC", str(falso))
    assert pf.dvc_identity(RAIZ) == {"executable": str(falso), "version": "9.9.9"}
    monkeypatch.setenv("VP_DVC", str(tmp_path / "no_existe"))
    with pytest.raises(pf.PreflightError, match="DVC gobernado"):
        pf.dvc_identity(RAIZ)


def _sync_all_hermetico(tmp_path: Path, env_extra: dict[str, str]) -> tuple[int, str, Path]:
    """`sync_all.sh` REAL sobre un repositorio de mentira: MLflow y git falsos, sin red ni artefactos vivos."""
    raiz = tmp_path / "repo"
    (raiz / "experiments").mkdir(parents=True)
    (raiz / "ante_nf" / "bin").mkdir(parents=True)
    shutil.copy(RAIZ / "experiments" / "sync_all.sh", raiz / "experiments" / "sync_all.sh")
    _ejecutable(raiz / "ante_nf" / "bin" / "python", "#!/bin/sh\nexit 0\n")
    registro = tmp_path / "dvc_calls.txt"
    _ejecutable(tmp_path / "dvc_falso", f'#!/bin/sh\necho "$@" >> "{registro}"\n')
    for nombre in ("models.dvc", "mlflow.db.dvc", ".gitignore"):
        (raiz / nombre).write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=raiz, check=True)
    fin = subprocess.run(
        ["bash", "experiments/sync_all.sh"],
        cwd=raiz,
        capture_output=True,
        text=True,
        timeout=120,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "SYNC_PUBLISH": "0", **env_extra},
    )
    return fin.returncode, fin.stdout + fin.stderr, registro


def test_RED_sync_all_rehashea_con_el_dvc_gobernado_y_no_con_el_del_PATH(tmp_path: Path) -> None:
    """Contra `9c5b908`, `dvc` a secas no está en el PATH mínimo y el guion muere en 127."""
    rc, salida, registro = _sync_all_hermetico(tmp_path, {"VP_DVC": str(tmp_path / "dvc_falso")})
    assert rc == 0, salida
    assert registro.read_text(encoding="utf-8").splitlines() == ["add models mlflow.db"]


def test_sync_all_falla_cerrado_sin_dvc_gobernado(tmp_path: Path) -> None:
    rc, salida, registro = _sync_all_hermetico(tmp_path, {})
    assert rc != 0 and "DVC gobernado" in salida
    assert not registro.exists()


def test_el_runbook_exige_el_dvc_gobernado_antes_de_sellar() -> None:
    vivas = "\n".join(ln for ln in RUNBOOK.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#"))
    assert 'export VP_DVC="${VP_DVC:-$PWD/ante/bin/dvc}"' in vivas
    assert vivas.index("-m tools.check_dvc_lock_fresh") < vivas.index("tools.campaign_preflight --out")


# ═══════════════════════════════ 3 · los productores escriben en el staging y el promotor lo promueve
def _limpiar_servido(modelo: str) -> None:
    """Si un productor viejo escribiera en el árbol servido (RED), no dejar rastro en el repositorio."""
    for tabla in ("FAD", "DFF"):
        for clase in ("local", "global"):
            shutil.rmtree(RAIZ / "models" / tabla / clase / modelo, ignore_errors=True)


def test_RED_el_productor_local_escribe_en_el_staging_de_la_campana(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("darts")
    import pandas as pd
    import save_finalists as sf

    staging = tmp_path / "staging"
    monkeypatch.setenv("VP_MODELS_DIR", str(staging))
    monkeypatch.setenv("VP_MODELS_MANIFEST", str(staging / "manifest.jsonl"))
    monkeypatch.setenv("CAMPAIGN_SHA", "b" * 40)
    monkeypatch.setattr(
        sf.dataset, "list_series", lambda **kw: pd.DataFrame({"country": ["mexico"], "category": ["F1"]})
    )
    monkeypatch.setattr(sf.dataset, "load_series", lambda *a, **k: pd.Series([1.0, 2.0]))
    monkeypatch.setattr(sf.models, "to_timeseries", lambda raw: raw)

    class _Modelo:
        def fit(self, *a, **k):
            return self

    class _SinCovariables:
        def __init__(self, name):
            pass

        def covariates(self, ts, raw):
            return None

    monkeypatch.setattr(sf.models, "build_model", lambda name, table: _Modelo())
    monkeypatch.setattr(sf, "FeatureBuilder", _SinCovariables)
    monkeypatch.setattr(sf.joblib, "dump", lambda obj, path, **kw: Path(path).write_bytes(b"pkl"))
    monkeypatch.setattr(sf, "LOCAL", ("modelo_r14",))
    manifiesto_servido = RAIZ / "models" / "manifest.jsonl"
    antes = manifiesto_servido.read_bytes() if manifiesto_servido.is_file() else None
    try:
        sf.main()
    finally:
        _limpiar_servido("modelo_r14")
        if antes is not None and manifiesto_servido.read_bytes() != antes:
            manifiesto_servido.write_bytes(antes)
    entradas = [json.loads(ln) for ln in (staging / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {(e["table"], e["model"], e["country"], e["category"]) for e in entradas} == {
        ("FAD", "modelo_r14", "mexico", "F1"),
        ("DFF", "modelo_r14", "mexico", "F1"),
    }
    assert all(Path(e["path"]).is_file() and Path(e["path"]).is_relative_to(staging) for e in entradas)


def test_RED_el_productor_deep_escribe_en_el_staging_de_la_campana(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("pandas")
    import types

    import pandas as pd

    class _NF:
        def __init__(self, models, freq):
            self.models = models

        def fit(self, df):
            return self

        def save(self, path, overwrite=True):
            Path(path).mkdir(parents=True, exist_ok=True)
            (Path(path) / "model.bin").write_bytes(b"nf")

    class _Clase:
        def __init__(self, **kw):
            self.kw = kw

    nf_mod = types.ModuleType("neuralforecast")
    nf_mod.NeuralForecast = _NF  # type: ignore[attr-defined]
    modelos = types.ModuleType("neuralforecast.models")
    for nombre in ("NHITS", "BiTCN", "PatchTST", "TiDE"):
        setattr(modelos, nombre, type(nombre, (_Clase,), {}))
    monkeypatch.setitem(sys.modules, "neuralforecast", nf_mod)
    monkeypatch.setitem(sys.modules, "neuralforecast.models", modelos)
    if importlib.util.find_spec("torch") is None:
        torch = types.ModuleType("torch")
        torch.set_num_threads = lambda n: None  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "torch", torch)

    import hpo_winner_receipt as hwr
    import save_finalists_deep as sfd

    staging = tmp_path / "staging"
    monkeypatch.setenv("VP_MODELS_DIR", str(staging))
    monkeypatch.setenv("VP_MODELS_MANIFEST", str(staging / "manifest.jsonl"))
    monkeypatch.setenv("CAMPAIGN_SHA", "b" * 40)
    monkeypatch.setattr(hwr, "load_and_accredit", lambda *a, **k: {"input_size": 3, "accelerator": "cpu"})
    monkeypatch.setattr(
        sfd,
        "load_panel",
        lambda table, block: pd.DataFrame(
            {
                "unique_id": ["a/family/F1"] * 3,
                "ds": pd.date_range("2024-01-01", periods=3, freq="MS"),
                "y": [1.0, 2.0, 3.0],
            }
        ),
    )
    try:
        sfd.main()
    finally:
        for nombre in (*sfd.DET, "AutoBiTCN"):
            _limpiar_servido(nombre)
    entradas = [json.loads(ln) for ln in (staging / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {(e["table"], e["model"]) for e in entradas} == {
        (t, m) for t in ("FAD", "DFF") for m in (*sfd.DET, "AutoBiTCN")
    }
    assert all(Path(e["path"]).is_dir() and Path(e["path"]).is_relative_to(staging) for e in entradas)


def _staging_completo(raiz: Path, cid: str, series: dict[str, list[tuple[str, str]]]) -> Path:
    """Un staging con exactamente lo que el registro y el catálogo exigen, con rutas relativas a `raiz`."""
    import promote_finalists as prom

    staging = raiz / "models" / ".staging" / cid
    esperado = prom.expected_models()
    lineas = []
    for tabla, ss in series.items():
        for modelo in esperado["local"]:
            for c, k in ss:
                out = staging / tabla / "local" / modelo / f"{c}_{k}" / "model.pkl"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"pkl")
                lineas.append({"model": modelo, "table": tabla, "type": "local", "country": c, "category": k, "path": str(out.relative_to(raiz)), "git_sha": "b" * 7, "panel_hash": "0" * 12})  # fmt: skip
        for modelo in esperado["global"]:
            out = staging / tabla / "global" / modelo
            out.mkdir(parents=True, exist_ok=True)
            (out / "model.bin").write_bytes(b"nf")
            lineas.append({"model": modelo, "table": tabla, "type": "global_deep", "path": str(out.relative_to(raiz)), "git_sha": "b" * 7, "panel_hash": "0" * 12})  # fmt: skip
    (staging / "manifest.jsonl").write_text("".join(json.dumps(e) + "\n" for e in lineas), encoding="utf-8")
    return staging


SERIES = {"FAD": [("mexico", "F1"), ("india", "F2A")], "DFF": [("mexico", "F1"), ("india", "F2A")]}


def test_RED_el_promotor_promueve_tambien_el_manifiesto_con_rutas_del_arbol_servido(tmp_path: Path) -> None:
    """Contra `9c5b908` las tablas se movían y `models/manifest.jsonl` seguía siendo el de la añada anterior."""
    import promote_finalists as prom

    raiz = tmp_path / "repo"
    viejo = raiz / "models" / "FAD" / "local" / "arima" / "viejo"
    viejo.mkdir(parents=True)
    (viejo / "model.pkl").write_bytes(b"viejo")
    (raiz / "models" / "manifest.jsonl").write_text(
        '{"model": "arima", "type": "local", "path": "models/FAD/local/arima/viejo/model.pkl"}\n', encoding="utf-8"
    )
    staging = _staging_completo(raiz, "rederiv_b_1", SERIES)

    recibo = prom.promote(staging, campaign_id="rederiv_b_1", root=raiz, series=SERIES)

    entradas = [json.loads(ln) for ln in (raiz / "models" / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(entradas) == 2 * (2 * len(prom.expected_models()["local"]) + len(prom.expected_models()["global"]))
    assert all(e["path"].startswith("models/") and ".staging" not in e["path"] for e in entradas)
    assert all((raiz / e["path"]).exists() for e in entradas)
    assert not staging.exists()
    retirados = list((raiz / "models" / ".retired").glob("*_rederiv_b_1"))
    assert (
        len(retirados) == 1
        and (retirados[0] / "manifest.jsonl").is_file()
        and (retirados[0] / "FAD" / "local" / "arima" / "viejo").is_dir()
    )
    assert json.loads(recibo.read_text(encoding="utf-8"))["campaign_id"] == "rederiv_b_1"
    assert prom.fresh_receipt("rederiv_b_1", raiz)["coverage"]["series_por_tabla"] == {"FAD": 2, "DFF": 2}


@pytest.mark.parametrize(
    "torcer,motivo",
    [
        (lambda es: es.pop(0), "faltan"),  # una serie de un modelo local
        (lambda es: es.append({**es[0], "model": "ets"}), "sobran"),  # un no persistible
        (lambda es: es.append(dict(es[0])), "DUPLICADA"),
        (lambda es: [es.pop() for _ in range(len(es)) if es[-1]["type"] == "global_deep"], "global: faltan"),
    ],
)
def test_RED_el_promotor_exige_cada_serie_y_cada_global_sin_sobrantes(tmp_path: Path, torcer, motivo: str) -> None:
    """Contra `9c5b908` la cobertura se comparaba por pares (tabla, modelo): una serie ausente pasaba."""
    import promote_finalists as prom

    raiz = tmp_path / "repo"
    staging = _staging_completo(raiz, "rederiv_b_2", SERIES)
    manifiesto = staging / "manifest.jsonl"
    entradas = [json.loads(ln) for ln in manifiesto.read_text(encoding="utf-8").splitlines()]
    torcer(entradas)
    manifiesto.write_text("".join(json.dumps(e) + "\n" for e in entradas), encoding="utf-8")
    with pytest.raises(prom.CoverageError, match=motivo):
        prom.promote(staging, campaign_id="rederiv_b_2", root=raiz, series=SERIES)
    assert staging.exists() and not (raiz / "models" / "coverage_receipt.json").exists()


def test_RED_el_orquestador_de_finalistas_deja_un_arbol_servido_acreditado(tmp_path: Path) -> None:
    """`save_finalists.sh` REAL con productores de mentira que honran las variables y el promotor REAL.

    Contra `9c5b908`, aunque los productores escribieran en el staging, `[2b/4]` no promovía el
    manifiesto y el promotor no admitía otra raíz que la del repositorio.
    """
    pytest.importorskip("duckdb")
    if not (RAIZ / "data" / "processed" / "visapredict.duckdb").is_file():
        pytest.skip("sin almacén DuckDB: el promotor deriva el catálogo de series de ahí")
    raiz = tmp_path / "repo"
    (raiz / "experiments").mkdir(parents=True)
    shutil.copy(RAIZ / "experiments" / "save_finalists.sh", raiz / "experiments" / "save_finalists.sh")
    _ejecutable(raiz / "experiments" / "sync_all.sh", "#!/bin/sh\nexit 0\n")
    stub = tmp_path / "productor.py"
    stub.write_text(
        textwrap.dedent(
            f"""
            import json, os, sys
            from pathlib import Path
            sys.path.insert(0, {str(RAIZ)!r})
            from vp_model import config, dataset
            from vp_model.model_registry import GLOBAL_MODELS, LOCAL_MODELS, PERSISTED_MODELS
            raiz = Path({str(raiz)!r}); staging = Path(os.environ["VP_MODELS_DIR"]); staging = staging if staging.is_absolute() else raiz / staging
            clase = sys.argv[1]
            with open(os.environ["VP_MODELS_MANIFEST"] if os.path.isabs(os.environ["VP_MODELS_MANIFEST"]) else raiz / os.environ["VP_MODELS_MANIFEST"], "a") as fh:
                for tabla in config.TABLES:
                    if clase == "local":
                        cat = dataset.list_series(table=tabla, block="family", countries=config.PILOT_COUNTRIES, db_path={str(RAIZ / "data" / "processed" / "visapredict.duckdb")!r})
                        for m in (x for x in LOCAL_MODELS if x in PERSISTED_MODELS):
                            for r in cat.itertuples():
                                out = staging / tabla / "local" / m / f"{{r.country}}_{{r.category}}" / "model.pkl"
                                out.parent.mkdir(parents=True, exist_ok=True); out.write_bytes(b"pkl")
                                fh.write(json.dumps({{"model": m, "table": tabla, "type": "local", "country": r.country, "category": r.category, "path": str(out.relative_to(raiz)), "git_sha": "b" * 7, "panel_hash": "0" * 12}}) + "\\n")
                    else:
                        for m in (x for x in GLOBAL_MODELS if x in PERSISTED_MODELS):
                            out = staging / tabla / "global" / m
                            out.mkdir(parents=True, exist_ok=True); (out / "model.bin").write_bytes(b"nf")
                            fh.write(json.dumps({{"model": m, "table": tabla, "type": "global_deep", "path": str(out.relative_to(raiz)), "git_sha": "b" * 7, "panel_hash": "0" * 12}}) + "\\n")
            """
        ),
        encoding="utf-8",
    )
    despacho = textwrap.dedent(
        f"""
        #!/bin/sh
        case "$1" in
          experiments/save_finalists_deep.py) exec "{sys.executable}" "{stub}" global ;;
          experiments/save_finalists.py) exec "{sys.executable}" "{stub}" local ;;
          experiments/promote_finalists.py) shift; exec "{sys.executable}" "{RAIZ / "experiments" / "promote_finalists.py"}" "$@" --root "{raiz}" ;;
          *) exit 0 ;;
        esac
        """
    ).lstrip()
    for venv in ("ante", "ante_nf"):
        (raiz / venv / "bin").mkdir(parents=True)
        _ejecutable(raiz / venv / "bin" / "python", despacho)
    fin = subprocess.run(
        ["bash", "experiments/save_finalists.sh"],
        cwd=raiz,
        capture_output=True,
        text=True,
        timeout=600,
        # el orquestador real exporta `PYTHONPATH="$PWD:…"`; aquí se le da además la raíz del repo, que es
        # de donde el promotor importa `vp_model` cuando corre con `--root` sobre el temporal
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "CAMPAIGN_ID": "rederiv_b_3", "PYTHONPATH": str(RAIZ)},
    )
    assert fin.returncode == 0, fin.stdout + fin.stderr
    entradas = [json.loads(ln) for ln in (raiz / "models" / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(entradas) == 2 * (25 * 5 + 5), len(entradas)
    assert all(e["path"].startswith("models/") and (raiz / e["path"]).exists() for e in entradas)
    assert not (raiz / "models" / ".staging").exists()
    assert (
        json.loads((raiz / "models" / "coverage_receipt.json").read_text(encoding="utf-8"))["campaign_id"]
        == "rederiv_b_3"
    )


# ═══════════════════════════════ 4 · los re-entrenos `--config` acreditan la ganadora
def _campana_de_mentira(tmp_path: Path) -> Path:
    raiz = tmp_path / "repo"
    (raiz / "reports" / "campaign").mkdir(parents=True)
    (raiz / "data" / "processed").mkdir(parents=True)
    panel = raiz / "data" / "processed" / "visa_panel_long.csv"
    panel.write_text("country,category,table,value\nmexico,EB2,FAD,1\n", encoding="utf-8")
    git = ["git", "-C", str(raiz)]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    subprocess.run([*git, "-c", "user.email=e@l", "-c", "user.name=e", "add", "-A"], check=True)
    subprocess.run([*git, "-c", "user.email=e@l", "-c", "user.name=e", "commit", "-qm", "panel"], check=True)
    head = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    import hashlib

    (raiz / "reports" / "campaign" / "campaign.json").write_text(
        json.dumps(
            {
                "campaign_id": "rederiv_abc1234_20260915T000000",
                "source_git_sha": head,
                "panel_sha256": "sha256:" + hashlib.sha256(panel.read_bytes()).hexdigest(),
                "status": "running",
            }
        ),
        encoding="utf-8",
    )
    return raiz


def test_RED_los_reentrenos_config_acreditan_la_ganadora_y_no_omiten_ausentes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("pandas")
    import hpo_winner_receipt as hwr
    import run_global_deep as rgd

    monkeypatch.setenv("VP_DEEP_ACCEL", "cpu")
    raiz = _campana_de_mentira(tmp_path)
    plantilla = "reports/campaign/hpo_deep_best_FAD_Auto{model}.json"
    with pytest.raises(hwr.WinnerReceiptError):  # antes: «se omite» y seguía
        rgd._accredited_configs(["BiTCN"], plantilla, "FAD", root=raiz)
    ganadora = raiz / "reports" / "campaign" / "hpo_deep_best_FAD_AutoBiTCN.json"
    ganadora.write_text(json.dumps({"input_size": 18, "accelerator": "cpu", "h": 1, "loss": "x"}), encoding="utf-8")
    with pytest.raises(hwr.WinnerReceiptError, match="recibo"):  # ganadora sin recibo
        rgd._accredited_configs(["BiTCN"], plantilla, "FAD", root=raiz)
    ident = hwr.campaign_identity(raiz)
    hwr.write_receipt(
        ganadora,
        campaign_id=ident["campaign_id"],
        code_sha=ident["source_git_sha"],
        panel_sha256=ident["panel_sha256"],
        table="FAD",
        model="AutoBiTCN",
        accelerator_in_artifact="cpu",
    )
    assert rgd._accredited_configs(["BiTCN"], plantilla, "FAD", root=raiz) == {
        "BiTCN": {"input_size": 18, "accelerator": "cpu"}
    }
    ganadora.write_text(json.dumps({"input_size": 99, "accelerator": "cpu"}), encoding="utf-8")
    with pytest.raises(hwr.WinnerReceiptError, match="cambió"):
        rgd._accredited_configs(["BiTCN"], plantilla, "FAD", root=raiz)


# ═══════════════════════════════ 5 · el trap mata a los descendientes aunque bash no sea líder
def test_el_trap_mata_a_los_descendientes_sin_depender_del_grupo(tmp_path: Path) -> None:
    guion = RUNBOOK.read_text(encoding="utf-8")
    funcion = re.search(r"^kill_descendants\(\) \{\n.*?^\}\n", guion, flags=re.M | re.S)
    assert funcion, "la función real de matanza no está en el runbook"
    pids = tmp_path / "pids"
    ensayo = tmp_path / "ensayo.sh"
    ensayo.write_text(
        "#!/bin/bash\n"
        + funcion.group(0)
        + f'sleep 300 & echo $! >> "{pids}"\n'
        + f"bash -c 'sleep 300 & echo $! >> \"{pids}\"; wait' &\n"
        + 'sleep 0.5\nkill_descendants "$$"\nsleep 0.5\nexit 0\n',
        encoding="utf-8",
    )
    subprocess.run(["bash", str(ensayo)], check=True, timeout=30)
    vivos = []
    for pid in (int(x) for x in pids.read_text(encoding="utf-8").split()):
        try:
            os.kill(pid, 0)
            vivos.append(pid)
        except ProcessLookupError:
            pass
        except PermissionError:
            vivos.append(pid)
    for pid in vivos:
        os.kill(pid, 9)
    assert not vivos, f"descendientes vivos tras el trap: {vivos}"
