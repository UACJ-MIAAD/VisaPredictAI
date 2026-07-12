"""Telemetría transaccional de campañas (A6) — thin wrapper sobre ``vp_data.tracking``.

Context-manager para runners de campaña: mide duración, RSS pico, memoria GPU (si hay
torch+CUDA), tamaño de artefactos, acumula warnings y captura la excepción TIPADA si el
bloque falla — y SIEMPRE emite un record v2 al staging JSONL (status ``ok``/``failed``),
con ``pipeline_run_id`` en el 100 % de los records (lo sella ``vp_data.tracking.log_run``).

Sin dependencias del extra ``model`` (stdlib + vp_data): importa igual en el job base de
CI que en los venv de campaña. La adopción por los runners (``run_global_deep`` /
``run_global_gbm``) es de otra oleada; este módulo solo aporta el helper.

Uso:
    from vp_model.tracking import track_run

    with track_run("pool_local_FAD", "ets_mexico_F1", params={"model": "ets"}) as run:
        ...entrenar/evaluar...
        run.log_metric("hold_mase", 0.114)
        run.warn("convergence: 2 restarts")
        run.add_artifact("models/FAD/ets_mexico_F1.pkl")
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from vp_data import tracking as base


def _rss_peak_mb() -> float | None:
    """RSS pico del proceso en MB (``ru_maxrss``: bytes en macOS, KB en Linux)."""
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(peak / (1 << 20) if sys.platform == "darwin" else peak / 1024, 1)
    except ImportError, OSError, AttributeError, ValueError:  # telemetría jamás aborta la corrida
        return None


def _gpu_mem_mb() -> float | None:
    """Memoria GPU pico (MB) si torch+CUDA están disponibles; None si no aplica."""
    try:
        import torch

        if torch.cuda.is_available():
            return round(torch.cuda.max_memory_allocated() / (1 << 20), 1)
    except Exception:  # noqa: BLE001
        pass
    return None


class TrackedRun:
    """Acumulador mutable que el bloque ``with`` va llenando (métricas/warnings/artefactos)."""

    def __init__(self, params: dict | None = None, tags: dict | None = None) -> None:
        self.params: dict = dict(params or {})
        self.metrics: dict = {}
        self.tags: dict = dict(tags or {})
        self.warnings: list[str] = []
        self.artifacts: list[str] = []

    def log_metric(self, key: str, value: float) -> None:
        self.metrics[key] = value

    def log_metrics(self, metrics: dict) -> None:
        self.metrics.update(metrics)

    def warn(self, message: str) -> None:
        self.warnings.append(str(message))

    def add_artifact(self, path: str | Path) -> None:
        self.artifacts.append(str(path))

    def artifact_bytes(self) -> int | None:
        """Tamaño total (bytes) de los artefactos que existen en disco; None si ninguno."""
        sizes = []
        for a in self.artifacts:
            p = Path(a) if Path(a).is_absolute() else base.ROOT / a
            if p.is_file():
                sizes.append(p.stat().st_size)
        return sum(sizes) if sizes else None


@contextmanager
def track_run(
    experiment: str,
    run_name: str,
    params: dict | None = None,
    tags: dict | None = None,
    *,
    recipe_version: str | None = None,
    seed: int | None = None,
    data_hash: str | None = None,
) -> Iterator[TrackedRun]:
    """Context-manager de telemetría: SIEMPRE loguea un record v2, incluso si el bloque falla.

    Fallo ⇒ ``telemetry.status="failed"`` + ``telemetry.exception`` tipada y la excepción
    se RE-LANZA (el tracking observa, no traga errores).
    """
    run = TrackedRun(params, tags)
    t0 = time.monotonic()
    error: BaseException | None = None
    try:
        yield run
    except BaseException as exc:
        error = exc
        raise
    finally:
        telemetry = {
            "status": "failed" if error is not None else "ok",
            "duration_s": round(time.monotonic() - t0, 3),
            "rss_peak_mb": _rss_peak_mb(),
            "gpu_mem_mb": _gpu_mem_mb(),
            "artifact_bytes": run.artifact_bytes(),
            "warnings": run.warnings,
            "exception": ({"type": type(error).__name__, "message": str(error)[:500]} if error is not None else None),
        }
        try:
            base.log_run(
                experiment,
                run_name,
                run.params,
                run.metrics,
                tags=run.tags,
                artifacts=run.artifacts,
                data_hash=data_hash,
                recipe_version=recipe_version,
                seed=seed,
                telemetry=telemetry,
            )
        except Exception as log_exc:  # noqa: BLE001 — no enmascarar la excepción original
            if error is None:
                raise
            print(f"[vp_model.tracking] log_run failed after run error: {log_exc!r}", file=sys.stderr)
