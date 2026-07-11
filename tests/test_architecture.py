"""E1 (plan auditoría 2026-07-11): el mapa de dependencias, con dientes.

Las capas y su dirección de imports (verificadas por AST, no por grep de substrings —
`pipeline_run_id` matcheaba "pipeline" en un grep ingenuo):

    vp_data   (dominio de datos: parseo, limpieza, config, tracking)  → solo stdlib+libs
    pipeline  (DAG ejecutable de datos)                               → vp_data
    vp_model  (dominio de modelado: métricas, ledger, promoción…)     → vp_data
    tools     (gates/CLIs)                                            → vp_data, vp_model
    experiments (entrypoints/orquestación)                            → cualquiera

Puertos (I/O detrás de una sola puerta): la RED vive exclusivamente en
``vp_data.visa_common.get_soup`` y ``pipeline.freeze_snapshots`` — reglas de visas,
métricas y postproceso se prueban sin red/DVC/MLflow (esta suite corre offline).
El reloj es inyectable donde importa (``ledger.stamp_rows(frozen_at=…)``); el tracking
es un puerto JSONL append-only (MLflow es un adapter histórico vía ``sync_mlflow``).
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# capa → módulos-tope PROHIBIDOS de importar
FORBIDDEN: dict[str, set[str]] = {
    "vp_data": {"vp_model", "pipeline", "experiments", "tools"},
    "pipeline": {"vp_model", "experiments", "tools"},
    "vp_model": {"pipeline", "experiments", "tools"},
    "tools": {"experiments"},
}

# la RED solo detrás de estos módulos (el resto del sistema se prueba offline)
NETWORK_PORTS = {"vp_data/visa_common.py", "pipeline/freeze_snapshots.py"}
NETWORK_LIBS = {"requests", "urllib", "http", "socket"}


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    tops: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            tops.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            tops.add(node.module.split(".")[0])
    return tops


def _layer_files(layer: str) -> list[Path]:
    return sorted((ROOT / layer).glob("*.py"))


def test_import_direction_between_layers() -> None:
    violations = []
    for layer, banned in FORBIDDEN.items():
        for f in _layer_files(layer):
            bad = _imports_of(f) & banned
            if bad:
                violations.append(f"{f.relative_to(ROOT)} importa {sorted(bad)}")
    assert not violations, "dirección de capas rota:\n" + "\n".join(violations)


def test_network_only_behind_its_ports() -> None:
    violations = []
    for layer in ("vp_data", "pipeline", "vp_model", "tools"):
        for f in _layer_files(layer):
            rel = str(f.relative_to(ROOT))
            if rel in NETWORK_PORTS:
                continue
            bad = _imports_of(f) & NETWORK_LIBS
            if bad:
                violations.append(f"{rel} importa {sorted(bad)}")
    assert not violations, "red fuera de sus puertos:\n" + "\n".join(violations)


def test_domain_logic_needs_no_mlflow_or_dvc() -> None:
    """Métricas/reglas/postproceso importables sin MLflow ni DVC instalados como tal:
    ningún módulo de las capas de dominio importa mlflow/dvc directamente (el puerto
    de tracking es JSONL; sync_mlflow vive en experiments/)."""
    violations = []
    for layer in ("vp_data", "pipeline", "vp_model"):
        for f in _layer_files(layer):
            bad = _imports_of(f) & {"mlflow", "dvc"}
            if bad:
                violations.append(f"{f.relative_to(ROOT)} importa {sorted(bad)}")
    assert not violations, "dominio acoplado a mlflow/dvc:\n" + "\n".join(violations)
