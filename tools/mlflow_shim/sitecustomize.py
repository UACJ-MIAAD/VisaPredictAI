"""Shim py3.14: mlflow importa Traversable de importlib.abc (removido en 3.14; está en
importlib.resources.abc). Se carga vía PYTHONPATH=tools/mlflow_shim antes de importar mlflow."""

import importlib.abc

if not hasattr(importlib.abc, "Traversable"):
    import importlib.resources.abc

    importlib.abc.Traversable = importlib.resources.abc.Traversable
