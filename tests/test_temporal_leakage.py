"""US-F1 — pruebas METAMÓRFICAS de fuga temporal en la transformación de features.

Propiedad central: **mutar cualquier valor F POSTERIOR a un origen no cambia NI una
feature NI un pronóstico en/antes de ese origen**. Bajo la interpolación lineal
bidireccional previa esto FALLABA para los orígenes dentro de un hueco (el valor
interpolado de un mes m ≤ origen usaba el bracket observado FUTURO t3 > origen);
bajo el relleno causal (LOCF, ``preprocess.to_regular_monthly_causal``) la propiedad
se cumple para TODOS los orígenes con una sola serie transformada.

Las fixtures cubren huecos de longitud 1, 3 y >3 (el antiguo cap MAX_INTERPOLABLE_GAP
distinguía ≤3 de >3; la política causal debe ser inmune en ambos regímenes). La
prueba de "filo" (sharpness) reimplementa la transformación ANTIGUA inline y verifica
que la propiedad metamórfica la ATRAPA — así este archivo no puede pasar en verde
por ser trivialmente débil.

Corre sin el almacén DuckDB: las series son sintéticas y ``dataset.load_series`` se
monkeypatcha (mismo patrón que test_tune_brutal). Sin el extra ``model`` el archivo
se omite de la colección vía ``conftest.collect_ignore`` (mismo patrón que
test_eda_preprocess).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from vp_model import dataset, missingness, models, walkforward
from vp_model.feature_builder import FeatureBuilder

# La serie sintética debe ser evaluable: 96 >= MIN_TRAIN['FAD'](60) + HOLDOUT(24) + BUFFER(6).
N = 96
GAP_START = 70  # el hueco arranca DENTRO de la región de pronóstico (orígenes >= 60)


def _series_with_gap(gap_len: int) -> tuple[pd.Series, pd.DatetimeIndex]:
    """Serie F sintética (tendencia + onda) con un hueco de ``gap_len`` meses."""
    idx = pd.date_range("2010-01-01", periods=N, freq="MS")
    vals = np.arange(N, dtype="float64") * 30.0 + 1000.0 + 40.0 * np.sin(np.arange(N) / 6.0)
    s = pd.Series(vals, index=idx)
    return s.drop(idx[GAP_START : GAP_START + gap_len]), idx


def _grid(raw: pd.Series) -> pd.Series:
    ts = models.to_timeseries(raw)
    return pd.Series(ts.values().flatten(), index=ts.time_index)


def _leaky_grid(raw: pd.Series) -> pd.Series:
    """La transformación PRE-F1 (contrafactual): interpolación lineal bidireccional.

    Reimplementada inline solo para las pruebas de filo — el código de producción ya
    no contiene esta ruta para el entrenamiento.
    """
    full = pd.date_range(raw.index.min(), raw.index.max(), freq="MS")
    return raw.reindex(full).astype("float64").interpolate(method="linear", limit_area="inside")


# --------------------------------------------------------------- features (rejilla)
@pytest.mark.parametrize("gap_len", [1, 3, 5])
def test_filled_grid_invariant_to_future_mutation(gap_len: int) -> None:
    """Mutar valores F posteriores al origen no cambia la rejilla en/antes del origen.

    El origen se planta DENTRO del hueco (el caso que fugaba): la mutación alcanza el
    bracket derecho del hueco, que la interpolación bidireccional usaba.
    """
    raw, idx = _series_with_gap(gap_len)
    origin = idx[GAP_START + gap_len - 1]  # último mes del hueco: t1 <= origen < t3
    base = _grid(raw)

    mutated = raw.copy()
    mutated[mutated.index > origin] += 5_000.0  # incluye el bracket derecho t3
    got = _grid(mutated)

    pre = base.index <= origin
    assert np.array_equal(base[pre].to_numpy(), got[pre].to_numpy()), (
        f"gap_len={gap_len}: la rejilla en/antes del origen cambió al mutar el futuro (fuga)"
    )
    # ... y la rejilla es idéntica a la serie F en los meses observados (LOCF no inventa).
    assert np.allclose(base.loc[raw.index].to_numpy(), raw.to_numpy())


@pytest.mark.parametrize("gap_len", [1, 3, 5])
def test_leaky_transform_is_caught_by_the_metamorphic_property(gap_len: int) -> None:
    """Filo de la prueba: la transformación ANTIGUA (bidireccional) VIOLA la propiedad.

    Si esta aserción dejara de fallar para la ruta leaky, la prueba metamórfica se
    habría vuelto trivial y ya no protegería nada.
    """
    raw, idx = _series_with_gap(gap_len)
    origin = idx[GAP_START + gap_len - 1]
    base = _leaky_grid(raw)
    mutated = raw.copy()
    mutated[mutated.index > origin] += 5_000.0
    got = _leaky_grid(mutated)
    pre = base.index <= origin
    assert not np.array_equal(base[pre].to_numpy(), got[pre].to_numpy()), (
        "la transformación bidireccional debería fugar en esta fixture (prueba sin filo)"
    )


def test_mask_covariates_are_causal() -> None:
    """Las máscaras MNAR en/antes del origen no dependen del futuro (valores NI presencia)."""
    raw, idx = _series_with_gap(3)
    origin = idx[GAP_START + 1]
    base = missingness.masking_features(raw)

    # 1) mutar VALORES futuros no toca las máscaras (dependen solo de la observabilidad)
    mutated = raw.copy()
    mutated[mutated.index > origin] += 999.0
    assert missingness.masking_features(mutated).equals(base)

    # 2) borrar una OBSERVACIÓN futura solo puede cambiar máscaras posteriores al origen
    dropped = raw.drop(idx[85])  # observación F posterior al hueco
    got = missingness.masking_features(dropped)
    pre = base.index[base.index <= origin]
    assert got.loc[pre].equals(base.loc[pre])


def test_masking_features_horizon_extension_is_honest() -> None:
    """La extensión a futuro (deploy) publica el estado de información honesto:
    observed=0 (aún no se publica) y el contador siguiendo su cuenta — jamás
    inventa observabilidad futura."""
    raw, _idx = _series_with_gap(3)
    base = missingness.masking_features(raw)
    mf = missingness.masking_features(raw, horizon=6)
    assert len(mf) == len(base) + 6
    assert mf.iloc[: len(base)].equals(base)  # el pasado no cambia
    ext = mf.iloc[len(base) :]
    assert (ext["observed"] == 0).all()
    assert list(ext["months_since_obs"]) == list(range(1, 7))  # sigue contando desde la última F


def test_covariates_horizon_extension_for_deploy() -> None:
    """FeatureBuilder.covariates(horizon=n) cubre predict(n) más allá del último
    boletín (el camino de despliegue de un GBM): calendario determinista + máscaras
    extendidas con el estado de información honesto."""
    raw, _idx = _series_with_gap(3)
    fe = FeatureBuilder("xgboost")
    ts = fe.to_timeseries(raw)
    cov = fe.covariates(ts, raw, horizon=12)
    assert cov is not None and len(cov) == len(ts) + 12
    assert list(cov.components) == list(fe.covariate_cols) + list(fe.mask_covariate_cols)
    tail = cov.values()[-12:, -1]  # months_since_obs extendido
    assert list(tail) == list(range(1, 13))


def test_covariates_fail_loud_without_raw() -> None:
    """F1: pedir covariables de un modelo con máscaras SIN la serie cruda debe
    reventar — degradar en silencio dejaría al GBM sin la señal de ausencia."""
    raw, _idx = _series_with_gap(1)
    ts = models.to_timeseries(raw)
    with pytest.raises(ValueError, match="máscaras MNAR"):
        FeatureBuilder("xgboost").covariates(ts)
    # los modelos sin máscaras siguen funcionando sin raw (API previa intacta)
    assert FeatureBuilder("ets").covariates(ts) is None


# ------------------------------------------------------------ pipeline (pronósticos)
@pytest.mark.parametrize(
    ("model_name", "gap_len"),
    [("naive1", 1), ("naive1", 3), ("naive1", 5), ("xgboost", 3)],
)
def test_forecasts_at_or_before_origin_invariant_to_future_mutation(
    model_name: str, gap_len: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pipeline completo (run_forecasts): mutar F posteriores al origen deja intactos
    los pronósticos en/antes del origen — incluye el GBM con máscaras MNAR (F1)."""
    raw, idx = _series_with_gap(gap_len)
    origin = idx[GAP_START + gap_len - 1]  # origen dentro del hueco (el caso que fugaba)

    def run(series: pd.Series) -> np.ndarray:
        monkeypatch.setattr(dataset, "load_series", lambda *a, **k: series)
        _ts, fc = walkforward.run_forecasts(model_name, "x", "F1", "FAD")
        keep = fc.time_index <= origin
        return fc.values().flatten()[keep]

    base = run(raw)
    assert len(base) > 0, "la fixture debe dejar orígenes antes del punto de mutación"
    mutated = raw.copy()
    mutated[mutated.index > origin] += 7_000.0
    got = run(mutated)
    assert np.allclose(base, got, rtol=0, atol=1e-8), (
        f"{model_name}/gap={gap_len}: el futuro alteró pronósticos en/antes del origen"
    )


def test_tree_features_before_origin_invariant_to_future_mutation() -> None:
    """Nivel feature del GBM: covariables (calendario + máscaras) en/antes del origen
    no cambian al mutar valores F futuros."""
    raw, idx = _series_with_gap(3)
    origin = idx[GAP_START + 1]
    fe = FeatureBuilder("xgboost")

    def feats(series: pd.Series) -> np.ndarray:
        ts = fe.to_timeseries(series)
        cov = fe.covariates(ts, series)
        assert cov is not None
        keep = cov.time_index <= origin
        return cov.values()[keep]

    base = feats(raw)
    mutated = raw.copy()
    mutated[mutated.index > origin] += 7_000.0
    assert np.array_equal(base, feats(mutated))
