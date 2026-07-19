#!/usr/bin/env python
"""B263/B266: lista POSITIVA ESTRUCTURAL de que los gates de gobernanza P0R.5 siguen cableados como PASOS NOMBRADOS y
EXACTOS del job `consistency` en `.github/workflows/ci.yml`.

La v1 (B263) validaba por SUBSTRING de texto `run:` → un workflow con `echo tools/check_reflection.py`, un comentario,
`|| true`, `if false`, un job distinto o `continue-on-error` la engañaba (B266). Ahora se parsea el YAML
ESTRUCTURALMENTE (con loader anti-claves-duplicadas) y se exige, para cada gate:

- existe un paso en `jobs.consistency.steps` con el `name` EXACTO;
- su `run` es el comando de UNA sola línea EXACTO (tras normalizar el newline final), no por substring;
- ese `name` aparece EXACTAMENTE una vez;
- el paso no tiene `if` ni `continue-on-error`, ni `shell`/`working-directory`/`env` que alteren Python/PATH;
- el job `consistency` no tiene `continue-on-error`.

Fail-closed ante workflow ausente/ilegible, YAML inválido, claves duplicadas, `steps` no-lista o el propio checker
ausente del conjunto requerido.
"""

from __future__ import annotations

import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WORKFLOW = ".github/workflows/ci.yml"
_JOB = "consistency"
# name EXACTO del paso → comando `run` de una línea EXACTO.
REQUIRED_STEPS = {
    "Commit frontier contract (fingerprint + autoridad)": "python tools/check_commit_frontier.py",
    "Positive reflection registry (identidad semántica)": "python tools/check_reflection.py",
    "Safe opens contract": "python tools/check_safe_opens.py",
    "Raw filesystem mutation contract": "python tools/check_raw_fs_mutations.py",
    "B233 historical diagnostic contract": "python -m tools.validate_b233_receipt",
    "P0R.5 governance gates wired": "python tools/check_p0r5_governance.py",
}
# claves de paso que NEUTRALIZARÍAN un gate (no permitidas en estos pasos).
_FORBIDDEN_STEP_KEYS = ("if", "continue-on-error", "shell", "working-directory", "env")


class _NoDupLoader(yaml.SafeLoader):
    """SafeLoader que RECHAZA claves de mapa duplicadas (PyYAML las acepta silenciosamente por defecto)."""


def _no_dup_mapping(loader: _NoDupLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    mapping: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(None, None, f"clave YAML duplicada: {key!r}", key_node.start_mark)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_NoDupLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_dup_mapping)


def problems() -> list[str]:
    path = os.path.join(ROOT, _WORKFLOW)
    try:
        with open(path, encoding="utf-8") as fh:
            doc = yaml.load(fh, Loader=_NoDupLoader)  # _NoDupLoader extiende SafeLoader (seguro) + anti-duplicados
    except OSError as exc:
        return [f"{_WORKFLOW}: ilegible ({exc}) (fail-closed B263)"]
    except yaml.YAMLError as exc:
        return [f"{_WORKFLOW}: YAML inválido/duplicado ({exc}) (fail-closed B266)"]
    if not isinstance(doc, dict):
        return [f"{_WORKFLOW}: raíz YAML no es un mapa (fail-closed B266)"]
    jobs = doc.get("jobs")
    job = jobs.get(_JOB) if isinstance(jobs, dict) else None
    if not isinstance(job, dict):
        return [f"{_WORKFLOW}: falta el job {_JOB!r} (fail-closed B266)"]
    if job.get("continue-on-error"):
        return [f"{_WORKFLOW}: el job {_JOB!r} tiene continue-on-error (B266)"]
    if "if" in job:  # un `if` a nivel de JOB saltaría el job completo (todos los gates) — ronda B
        return [f"{_WORKFLOW}: el job {_JOB!r} tiene un `if` de nivel de job (saltaría todos los gates) (B266)"]
    steps = job.get("steps")
    if not isinstance(steps, list):
        return [f"{_WORKFLOW}: jobs.{_JOB}.steps no es una lista (fail-closed B266)"]

    by_name: dict[str, list[dict]] = {}
    for st in steps:
        if isinstance(st, dict) and isinstance(st.get("name"), str):
            by_name.setdefault(st["name"], []).append(st)

    problems: list[str] = []
    for name, cmd in REQUIRED_STEPS.items():
        matches = by_name.get(name, [])
        if len(matches) != 1:
            problems.append(f"gate de gobernanza: el paso con name {name!r} aparece {len(matches)} veces (debe ser 1) en jobs.{_JOB} (B266)")  # fmt: skip
            continue
        step = matches[0]
        run = step.get("run")
        if not isinstance(run, str) or run.strip("\n") != cmd:
            problems.append(f"gate de gobernanza: el paso {name!r} debe correr EXACTAMENTE `{cmd}` (obtenido {run!r}) (B266)")  # fmt: skip
        for k in _FORBIDDEN_STEP_KEYS:
            if k in step:
                problems.append(f"gate de gobernanza: el paso {name!r} no puede llevar `{k}` (neutralizaría el gate) (B266)")  # fmt: skip
    return problems


def main() -> int:
    probs = problems()
    if probs:
        print("✗ gobernanza P0R.5 no cableada estructuralmente en CI:")
        for p in probs:
            print(f"  - {p}")
        return 1
    print(f"✓ los {len(REQUIRED_STEPS)} gates de gobernanza P0R.5 están cableados como pasos exactos de jobs.{_JOB}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
