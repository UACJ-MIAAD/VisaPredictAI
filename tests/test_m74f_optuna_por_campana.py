"""M74-F · deuda 5 — una campaña no reanuda los estudios Optuna de otra.

`tune.tune` crea los estudios con ``load_if_exists=True`` sobre una base sqlite compartida: dentro de
la MISMA campaña eso es lo que deja al rank-check y a la confirmación leer la búsqueda, pero entre
campañas hacía que la nueva heredara los trials de la anterior. Las pruebas usan bases temporales.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("darts")
optuna = pytest.importorskip("optuna")

from vp_model import tune  # noqa: E402


@pytest.fixture
def bases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(tune, "OPTUNA_DB", tmp_path / "optuna.db")
    monkeypatch.setattr(tune, "OPTUNA_CAMPAIGN_DIR", tmp_path / "optuna")
    return tmp_path


def test_fuera_de_campana_sigue_la_base_historica(bases: Path, monkeypatch) -> None:
    monkeypatch.delenv("CAMPAIGN_ID", raising=False)
    assert tune.default_storage_path() == bases / "optuna.db"


def test_dentro_de_campana_la_base_es_de_esa_campana(bases: Path, monkeypatch) -> None:
    monkeypatch.setenv("CAMPAIGN_ID", "rederiv_953cd00_20261006T073927")
    assert tune.default_storage_path() == bases / "optuna" / "rederiv_953cd00_20261006T073927.db"


@pytest.mark.parametrize("malo", ["../x", "a/b", ".oculto", "con espacio"])
def test_un_campaign_id_que_no_es_nombre_de_archivo_falla(bases: Path, monkeypatch, malo: str) -> None:
    monkeypatch.setenv("CAMPAIGN_ID", malo)
    with pytest.raises(ValueError, match="nombre de archivo seguro"):
        tune.default_storage_path()


def _estudio(n_trials: int) -> int:
    """Lo que hace `tune.tune`: create_study(load_if_exists=True) sobre el storage por defecto."""
    with tune._sqlite_storage(None) as st:
        s = optuna.create_study(study_name="hpo-x-FAD-family-v2", storage=st, load_if_exists=True)
        s.optimize(lambda t: t.suggest_float("a", 0, 1), n_trials=n_trials)
        return len(s.trials)


def test_RED_la_campana_nueva_no_hereda_los_trials_de_la_anterior(bases: Path, monkeypatch) -> None:
    """Contra `953cd00` la segunda campaña reanudaba el estudio: 3 + 2 = 5 trials."""
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    monkeypatch.setenv("CAMPAIGN_ID", "rederiv_a_1")
    assert _estudio(3) == 3
    monkeypatch.setenv("CAMPAIGN_ID", "rederiv_b_2")
    assert _estudio(2) == 2, "la campaña b vio los trials de la a"
    # control: DENTRO de la misma campaña el estudio sí se reanuda (rank-check y confirmación lo leen)
    monkeypatch.setenv("CAMPAIGN_ID", "rederiv_a_1")
    assert _estudio(1) == 4
    assert not (bases / "optuna.db").exists(), "la base histórica no se toca dentro de una campaña"
