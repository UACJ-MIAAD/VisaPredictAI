#!/usr/bin/env python
"""B263/B266/B271: valida ESTRUCTURALMENTE que los gates de gobernanza P0R.5 corren en un job DEDICADO, MÍNIMO y
SELLADO de `.github/workflows/ci.yml`, y que `ci-gate` depende de él.

Historia: B263 los cableó como pasos NOMBRADOS del job `consistency`; B266 exigió name+run exactos por YAML estructural;
pero el CONTEXTO del job (env/defaults/container/services/pasos previos que tocan GITHUB_PATH) podía neutralizar los
comandos exactos aunque el `run` no cambiara (B271). Ahora los gates viven en su propio job `p0r5-governance` y este
checker exige su forma COMPLETA: claves exactas del job (sin `if`/`continue-on-error`/`env`/`defaults`/`container`/
`services`/`strategy`/claves desconocidas), runner/timeout/permissions exactos, la SECUENCIA COMPLETA y ORDENADA de
pasos (checkout+setup-python pineados por SHA del registro positivo, `pip install pyyaml`, los 6 gates), claves exactas
por paso (ningún paso extra, ninguno que escriba GITHUB_PATH), y `ci-gate.needs` que incluya `p0r5-governance` con la
lógica que exige el success de todos sus needs. Loader anti-claves-duplicadas + anchors resueltos. Fail-closed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)  # B278: raíz del repo en sys.path para importar `tools.check_action_pins` en forma script
_WORKFLOW = ".github/workflows/ci.yml"
_ACTION_REGISTRY = "security/github_actions.json"
_JOB = "p0r5-governance"
_CI_GATE = "ci-gate"
# B278: SHA REVISADOS de las dos acciones bootstrap del job. La biyección constante↔registro↔paso impide falsificar
# registro y workflow a la vez: el SHA del registro positivo debe igualar EXACTAMENTE esta constante de código.
_BOOTSTRAP_ACTIONS = {
    "actions/checkout": "93cb6efe18208431cddfb8368fd83d5badbf9bfd",
    "actions/setup-python": "ece7cb06caefa5fff74198d8649806c4678c61a1",
}
_EXPECTED_JOB_KEYS = {"name", "runs-on", "timeout-minutes", "permissions", "steps"}
_EXPECTED_RUNNER = "ubuntu-24.04"
_EXPECTED_TIMEOUT = 10
_EXPECTED_PERMISSIONS = {"contents": "read"}
# los 6 gates, en ORDEN: (name exacto, comando `run` de una línea exacto)
_GATE_STEPS = (
    ("Commit frontier contract (fingerprint + autoridad)", "python tools/check_commit_frontier.py"),
    ("Positive reflection registry (identidad semántica)", "python tools/check_reflection.py"),
    ("Safe opens contract", "python tools/check_safe_opens.py"),
    ("Raw filesystem mutation contract", "python tools/check_raw_fs_mutations.py"),
    ("B233 historical diagnostic contract", "python -m tools.validate_b233_receipt"),
    ("P0R.5 governance gates wired", "python tools/check_p0r5_governance.py"),
)
# B275/B283: contrato EXACTO de `ci-gate` (nada de substrings sobre yaml.dump). Claves de job, runner PINEADO, timeout,
# permissions VACÍAS, set COMPLETO de needs y los dos pasos (con el PROGRAMA `run` COMPLETO del paso que falla, no sólo
# su última línea) se comparan estructuralmente contra estas constantes revisadas.
_CI_GATE_JOB_KEYS = {"name", "if", "needs", "runs-on", "timeout-minutes", "permissions", "steps"}
_CI_GATE_RUNNER = "ubuntu-24.04"
_CI_GATE_TIMEOUT = 5
_CI_GATE_PERMISSIONS: dict = {}
_CI_GATE_NEEDS = {
    "commit-policy",
    "lint-and-test",
    "model-tests",
    "consistency",
    "supply-chain",
    "deep-lock-install",
    "dvc-tool-install",
    "environment-contract",
    "environment-receipts",
    "campaign-bundle-contract",
    "p0r5-governance",
}
_CI_GATE_STEP0_NAME = "Fail if any required job was not success"
_CI_GATE_STEP0_IF = "${{ contains(needs.*.result, 'failure') || contains(needs.*.result, 'cancelled') || contains(needs.*.result, 'skipped') }}"  # fmt: skip
# B283: el PROGRAMA COMPLETO del paso que debe fallar (no basta con que la última línea sea `exit 1`: un `exit 0` previo
# lo neutralizaba).
_CI_GATE_STEP0_RUN = (
    'echo "resultados: ${{ join(needs.*.result, \', \') }}"\n'
    'echo "✗ ci-gate: al menos un job no terminó en success"\n'
    "exit 1\n"
)
_CI_GATE_STEP1_NAME = "All required jobs succeeded"
_CI_GATE_STEP1_RUN = 'echo "ci-gate OK - todos los jobs en success"'


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


def _action_uses() -> tuple[dict[str, str], list[str]]:
    """B278: deriva los pins de checkout/setup-python del registro POSITIVO validado ESTRICTAMENTE
    (`check_action_pins.load_registry`: claves superiores exactas, `schema_version` int, entradas EXACTAS
    `{sha, version, runtime}`, SHA 40-hex, versión no vacía, runtime `node24`, sin claves duplicadas) y EXIGE que su SHA
    sea EXACTAMENTE el de la constante de código `_BOOTSTRAP_ACTIONS`. Falsificar el registro y el workflow a la vez ya
    no pasa: el SHA del registro debe igualar la constante revisada. Frontera honesta: ningún gate DENTRO del job deshace
    una acción maliciosa YA ejecutada; la prevención previa a la ejecución es el SHA revisado + el ruleset + la revisión
    humana. Este gate detecta drift/inconsistencia, no es infalsificable."""
    from tools import (
        check_action_pins as action_pins,  # local: ROOT ya está en sys.path (bootstrap al cargar el módulo)
    )

    try:
        reg = action_pins.load_registry(Path(os.path.join(ROOT, _ACTION_REGISTRY)))
    except SystemExit as exc:
        return {}, [f"{_ACTION_REGISTRY}: registro inválido ({exc}) (fail-closed B278)"]
    except OSError as exc:
        return {}, [f"{_ACTION_REGISTRY}: ilegible ({exc}) (fail-closed B278)"]
    out: dict[str, str] = {}
    problems: list[str] = []
    for name, expected_sha in _BOOTSTRAP_ACTIONS.items():
        entry = reg.get(name)
        if not (isinstance(entry, dict) and entry.get("sha") == expected_sha):
            problems.append(f"{_ACTION_REGISTRY}: {name} sha != la constante _BOOTSTRAP_ACTIONS revisada ({expected_sha[:12]}…) (fail-closed B278)")  # fmt: skip
        else:
            out[name] = f"{name}@{expected_sha}"
    return ({}, problems) if problems else (out, [])


def _expected_steps(uses: dict[str, str]) -> list[dict]:
    return [
        {"uses": uses["actions/checkout"], "with": {"fetch-depth": 0}},
        {"uses": uses["actions/setup-python"], "with": {"python-version": "3.14"}},
        {"run": "pip install pyyaml==6.0.3"},
        # B278: el propio gate de pins de Actions corre DENTRO del job mínimo (offline), antes de los demás gates.
        {"name": "GitHub Actions positive registry (offline)", "run": "python tools/check_action_pins.py"},
        *[{"name": n, "run": c} for n, c in _GATE_STEPS],
    ]


def _step_problems(observed: list, expected: list[dict]) -> list[str]:
    problems: list[str] = []
    if not isinstance(observed, list) or len(observed) != len(expected):
        return [f"jobs.{_JOB}.steps debe ser una lista EXACTA de {len(expected)} pasos (obtenidos {len(observed) if isinstance(observed, list) else type(observed).__name__}) (B271)"]  # fmt: skip
    for i, (obs, exp) in enumerate(zip(observed, expected, strict=True)):
        if not isinstance(obs, dict):
            problems.append(f"jobs.{_JOB}.steps[{i}] no es un mapa (B271)")
        elif (
            obs != exp
        ):  # claves + valores EXACTOS (rechaza `env`/`if`/`shell`/GITHUB_PATH/comando alterado/paso extra)
            problems.append(f"jobs.{_JOB}.steps[{i}] != el paso exacto esperado (obtenido {sorted(obs)} / {obs.get('name') or obs.get('run') or obs.get('uses')!r}) (B271)")  # fmt: skip
    return problems


def problems() -> list[str]:
    try:
        with open(os.path.join(ROOT, _WORKFLOW), encoding="utf-8") as fh:
            doc = yaml.load(fh, Loader=_NoDupLoader)  # _NoDupLoader extiende SafeLoader (seguro) + anti-duplicados
    except OSError as exc:
        return [f"{_WORKFLOW}: ilegible ({exc}) (fail-closed B263)"]
    except yaml.YAMLError as exc:
        return [f"{_WORKFLOW}: YAML inválido/duplicado ({exc}) (fail-closed B266)"]
    if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), dict):
        return [f"{_WORKFLOW}: sin `jobs` mapa (fail-closed B271)"]
    jobs = doc["jobs"]
    uses, uerr = _action_uses()
    if uerr:
        return uerr

    job = jobs.get(_JOB)
    problems: list[str] = []
    if not isinstance(job, dict):
        return [f"{_WORKFLOW}: falta el job {_JOB!r} (fail-closed B271)"]
    if set(job.keys()) != _EXPECTED_JOB_KEYS:  # prohíbe if/continue-on-error/env/defaults/container/services/strategy
        problems.append(f"jobs.{_JOB}: claves != EXACTAMENTE {sorted(_EXPECTED_JOB_KEYS)} (obtenido {sorted(job)}) — sin env/defaults/container/if/etc. (B271)")  # fmt: skip
    else:
        if job["name"] != _JOB:
            problems.append(f"jobs.{_JOB}.name != {_JOB!r} (B271)")
        if job["runs-on"] != _EXPECTED_RUNNER:
            problems.append(f"jobs.{_JOB}.runs-on != {_EXPECTED_RUNNER!r} (B271)")
        if not (type(job["timeout-minutes"]) is int and job["timeout-minutes"] == _EXPECTED_TIMEOUT):
            problems.append(f"jobs.{_JOB}.timeout-minutes != {_EXPECTED_TIMEOUT} (B271)")
        if job["permissions"] != _EXPECTED_PERMISSIONS:
            problems.append(f"jobs.{_JOB}.permissions != {_EXPECTED_PERMISSIONS} (B271)")
        problems.extend(_step_problems(job["steps"], _expected_steps(uses)))

    problems.extend(_ci_gate_problems(jobs.get(_CI_GATE)))
    return problems


def _ci_gate_step_problems(steps: object) -> list[str]:
    """B275: DOS pasos exactos. steps[0] (el que falla) = claves {name, if, run}, `if` = predicado COMPLETO
    failure/cancelled/skipped sobre needs.*.result, y `run` termina en `exit 1`. steps[1] (el de éxito) = claves
    {name, run} EXACTAS (sin `if`/env/extras) con name+run exactos."""
    if not (isinstance(steps, list) and len(steps) == 2 and all(isinstance(s, dict) for s in steps)):
        return [f"jobs.{_CI_GATE}.steps debe ser EXACTAMENTE 2 pasos-mapa (B275)"]
    problems: list[str] = []
    fail, ok = steps
    if set(fail.keys()) != {"name", "if", "run"}:
        problems.append(f"jobs.{_CI_GATE}.steps[0]: claves != {{name, if, run}} (obtenido {sorted(fail)}) (B275)")
    else:
        if fail["name"] != _CI_GATE_STEP0_NAME:
            problems.append(f"jobs.{_CI_GATE}.steps[0].name != {_CI_GATE_STEP0_NAME!r} (B275)")
        if fail["if"] != _CI_GATE_STEP0_IF:
            problems.append(f"jobs.{_CI_GATE}.steps[0].if != el predicado EXACTO failure/cancelled/skipped sobre needs.*.result (obtenido {fail['if']!r}) (B275)")  # fmt: skip
        if fail["run"] != _CI_GATE_STEP0_RUN:  # B283: el PROGRAMA completo, no sólo la última línea (un `exit 0` previo neutralizaba)
            problems.append(f"jobs.{_CI_GATE}.steps[0].run != el programa EXACTO que termina en `exit 1` (obtenido {fail['run']!r}) (B283)")  # fmt: skip
    if set(ok.keys()) != {"name", "run"}:
        problems.append(f"jobs.{_CI_GATE}.steps[1]: claves != {{name, run}} — sin `if`/env/extras (obtenido {sorted(ok)}) (B275)")  # fmt: skip
    else:
        if ok["name"] != _CI_GATE_STEP1_NAME:
            problems.append(f"jobs.{_CI_GATE}.steps[1].name != {_CI_GATE_STEP1_NAME!r} (B275)")
        if ok["run"] != _CI_GATE_STEP1_RUN:
            problems.append(f"jobs.{_CI_GATE}.steps[1].run != {_CI_GATE_STEP1_RUN!r} (B275)")
    return problems


def _ci_gate_problems(gate: object) -> list[str]:
    """B275: `ci-gate` se valida por FORMA EXACTA, no por substring sobre `yaml.dump` (que un decoy `env` o comentario
    con 'needs.*.result' satisfacía mientras `if: ${{ false }}` neutralizaba el paso que debe fallar). Claves de job
    EXACTAS (sin env/defaults/permissions/container/services/strategy/continue-on-error/desconocidas), name/if/runs-on
    exactos, `needs` = set EXACTO (sin ausencias/extras/duplicados; retirar un job requerido cae aunque
    p0r5-governance siga) y los dos pasos exactos."""
    if not isinstance(gate, dict):
        return [f"{_WORKFLOW}: falta el job {_CI_GATE!r} o no es un mapa (B275)"]
    if set(gate.keys()) != _CI_GATE_JOB_KEYS:
        return [f"jobs.{_CI_GATE}: claves != EXACTAMENTE {sorted(_CI_GATE_JOB_KEYS)} (obtenido {sorted(gate)}) — sin env/defaults/container/if-de-job-extra/etc. (B275)"]  # fmt: skip
    problems: list[str] = []
    if gate["name"] != _CI_GATE:
        problems.append(f"jobs.{_CI_GATE}.name != {_CI_GATE!r} (B275)")
    if gate["if"] != "always()":
        problems.append(f"jobs.{_CI_GATE}.if != 'always()' exacto (obtenido {gate['if']!r}) (B275)")
    if gate["runs-on"] != _CI_GATE_RUNNER:  # B283: runner PINEADO (no 'latest')
        problems.append(f"jobs.{_CI_GATE}.runs-on != {_CI_GATE_RUNNER!r} (B283)")
    if not (type(gate["timeout-minutes"]) is int and gate["timeout-minutes"] == _CI_GATE_TIMEOUT):  # B283: timeout exacto (bool/float/0 caen)
        problems.append(f"jobs.{_CI_GATE}.timeout-minutes != {_CI_GATE_TIMEOUT} (B283)")
    if gate["permissions"] != _CI_GATE_PERMISSIONS:  # B283: permisos VACÍOS
        problems.append(f"jobs.{_CI_GATE}.permissions != {{}} vacío (obtenido {gate['permissions']!r}) (B283)")
    needs = gate["needs"]
    if not (isinstance(needs, list) and all(isinstance(n, str) for n in needs)):
        problems.append(f"jobs.{_CI_GATE}.needs no es una lista de strings (B275)")
    elif len(needs) != len(set(needs)):
        problems.append(f"jobs.{_CI_GATE}.needs tiene duplicados (B275)")
    elif set(needs) != _CI_GATE_NEEDS:
        problems.append(f"jobs.{_CI_GATE}.needs != el set EXACTO (falta {sorted(_CI_GATE_NEEDS - set(needs))}, sobra {sorted(set(needs) - _CI_GATE_NEEDS)}) (B275)")  # fmt: skip
    problems.extend(_ci_gate_step_problems(gate["steps"]))
    return problems


def main() -> int:
    probs = problems()
    if probs:
        print("✗ job de gobernanza P0R.5 no sellado en CI:")
        for p in probs:
            print(f"  - {p}")
        return 1
    print(f"✓ el job {_JOB!r} está sellado (mapa/pasos/orden exactos) y {_CI_GATE} depende de él ({_WORKFLOW})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
