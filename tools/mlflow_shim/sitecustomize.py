"""Shim py3.14: mlflow importa Traversable de importlib.abc (removido en 3.14; está en
importlib.resources.abc). Se carga vía PYTHONPATH=tools/mlflow_shim antes de importar mlflow."""

import importlib.abc as importlib_abc  # B324: alias del SUBMÓDULO (no liga la raíz `importlib`)

if not hasattr(importlib_abc, "Traversable"):
    import importlib.resources.abc as importlib_resources_abc

    importlib_abc.Traversable = importlib_resources_abc.Traversable
