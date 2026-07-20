"""B331: gate AST de autoridad del smoke deep (tools/check_deep_authority).

El fichero real DEBE pasar (el recibo se construye SÓLO en `certify_observation`, que carga el contrato canónico y no
acepta contrato del caller). Las regresiones adversariales inyectan fuente MANIPULADA que reintroduce el agujero de B331 y
verifican que el gate la CAZA."""

from __future__ import annotations

import tools.check_deep_authority as g

REAL = open("tools/deep_smoke.py", encoding="utf-8").read()  # noqa: SIM115


def test_real_deep_smoke_passes_authority_gate():
    assert g.problems(REAL) == []


def test_caller_contract_param_is_caught():
    tampered = REAL.replace(
        "def certify_observation(lock_rel: str, observation: DeepObservation)",
        "def certify_observation(lock_rel: str, observation: DeepObservation, *, contract: object = None)",
    )
    assert any("contrato del caller" in p for p in g.problems(tampered))


def test_emitter_without_load_contract_is_caught():
    tampered = REAL.replace(
        "contract = load_contract()  # AUTORIDAD CANÓNICA — nunca del caller (B331)",
        "contract = None  # tampered",
    )
    assert any("load_contract" in p for p in g.problems(tampered))


def test_receipt_built_in_evaluate_is_caught():
    leak = '    _leak = {"deep_smoke_contract_sha256": 1, "tensor_checksum": 2, "lock_sha256": 3}\n    return probs, {}'
    tampered = REAL.replace(
        "    return probs, {}  # B331: SIEMPRE recibo vacío — la certificación vive en certify_observation",
        leak,
        1,
    )
    probs = g.problems(tampered)
    assert any("EXACTAMENTE 1" in p or "evaluate" in p for p in probs), probs


def test_missing_emitter_is_caught():
    tampered = REAL.replace("def certify_observation(", "def _renamed_emitter(")
    assert any("falta la función emisora" in p for p in g.problems(tampered))
