"""B331/B335: gate AST de autoridad del smoke deep (tools/check_deep_authority).

El fichero real DEBE pasar (el recibo se construye SÓLO en `certify_runtime`, que acepta sólo `lock_rel`, observa y carga
el contrato por su cuenta, y ninguna función que reciba un `DeepObservation` construye recibo). Las regresiones
adversariales inyectan fuente MANIPULADA que reintroduce los agujeros de B331/B335 y verifican que el gate las CAZA."""

from __future__ import annotations

import pathlib

import tools.check_deep_authority as g

REAL = pathlib.Path("tools/deep_smoke.py").read_text(encoding="utf-8")


def test_real_deep_smoke_passes_authority_gate():
    assert g.problems(REAL) == []


def test_b335_emitter_with_observation_param_is_caught():
    tampered = REAL.replace(
        "def certify_runtime(lock_rel: str) -> tuple[list[str], dict]:",
        "def certify_runtime(lock_rel: str, observation: DeepObservation = None) -> tuple[list[str], dict]:",
    )
    assert any("SÓLO" in p for p in g.problems(tampered))


def test_b335_observation_param_function_building_receipt_is_caught():
    inj = (
        "def certify_observation(lock_rel: str, observation: DeepObservation) -> tuple[list[str], dict]:\n"
        '    receipt = {"deep_smoke_contract_sha256": 1, "tensor_checksum": 2, "lock_sha256": 3}\n'
        "    return [], receipt\n\n\n"
        "def certify_runtime(lock_rel: str)"
    )
    tampered = REAL.replace("def certify_runtime(lock_rel: str)", inj, 1)
    assert any("DeepObservation" in p and "inyect" in p for p in g.problems(tampered))


def test_b335_emitter_without_observe_runtime_is_caught():
    tampered = REAL.replace(
        "obs_problems, observation = observe_runtime(lock_rel)  # entorno REAL — nunca del caller (B335)",
        "obs_problems, observation = [], None",
        1,
    )
    assert any("observe_runtime" in p for p in g.problems(tampered))


def test_emitter_without_load_contract_is_caught():
    tampered = REAL.replace(
        "contract = load_contract()  # AUTORIDAD CANÓNICA — nunca del caller (B331)",
        "contract = None  # tampered",
    )
    assert any("load_contract" in p for p in g.problems(tampered))


def test_receipt_built_in_evaluate_is_caught():
    leak = '    _leak = {"deep_smoke_contract_sha256": 1, "tensor_checksum": 2, "lock_sha256": 3}\n    return probs, {}'
    tampered = REAL.replace(
        "    return probs, {}  # B331: SIEMPRE recibo vacío — la certificación vive en certify_runtime",
        leak,
        1,
    )
    probs = g.problems(tampered)
    assert any("EXACTAMENTE 1" in p or "evaluate" in p for p in probs), probs


def test_run_not_delegating_is_caught():
    tampered = REAL.replace("    return certify_runtime(lock_rel)", "    return [], {}")
    assert any("run" in p.lower() and "deleg" in p for p in g.problems(tampered))


def test_missing_emitter_is_caught():
    tampered = REAL.replace("def certify_runtime(", "def _renamed_emitter(")
    assert any("falta la función emisora" in p for p in g.problems(tampered))
