"""Adversarial del smoke deep (tools/deep_smoke.evaluate, P0R.4R2 · B322/B323).

Prueba la lógica PURA con valores inyectados (sin instalar el stack deep): la expectativa de plataforma/
torch se DERIVA del contrato (DEEP_RUNTIME) y el inventario esperado de un contrato INDEPENDIENTE
(`security/deep_smoke_contract.json`), no del llamador. Casos: lock no gobernado, plataforma/torch/versión
incorrectas, pip check rojo, contrato de lockset rojo, checksum no determinista, inventario observado que
OMITE/AGREGA respecto del contrato (B322), tipos inválidos, y happy path con receipt ligado (sha del lock +
del manifiesto + del contrato de inventario + commit).
"""

from __future__ import annotations

import pytest

import tools.deep_smoke as ds
import tools.lock_contracts as lc

CPU = "locks/deep-linux-x86_64-cpu.txt"
CONTRACT, CONTRACT_SHA = ds.load_contract()  # autoridad INDEPENDIENTE del inventario (B323)
CONTRACT_DISTS = [d for _, d in CONTRACT]


def _installed(lock_rel):
    pins = lc.pin_map((lc.ROOT / lock_rel).read_text())
    return {dist: (lc.DEEP_TORCH[lock_rel] if dist == "torch" else pins[lc._norm(dist)]) for dist in CONTRACT_DISTS}


def _kwargs(lock_rel, **over):
    rt = lc.DEEP_RUNTIME[lock_rel]
    base = dict(
        py_version="3.14.2",
        system=rt["system"],
        machine=rt["machine"],
        installed=_installed(lock_rel),
        torch_version=rt["torch"],
        pip_check_ok=True,
        checksum=83.0,
        contract_imports=CONTRACT,
        contract_sha=CONTRACT_SHA,
    )
    base.update(over)
    return base


def test_happy_path_receipt_is_lock_and_contract_bound():
    probs, receipt = ds.evaluate(CPU, **_kwargs(CPU))
    assert probs == []
    assert receipt["lock_sha256"].startswith("sha256:") and len(receipt["lock_sha256"]) == 71
    assert receipt["manifest_sha256"].startswith("sha256:")
    assert receipt["deep_smoke_contract_sha256"] == CONTRACT_SHA  # B322: recibo LIGADO al contrato de inventario
    assert list(receipt["versions"]) == CONTRACT_DISTS  # orden CANÓNICO del contrato
    assert receipt["commit_sha"] and receipt["torch_observed"] == lc.DEEP_TORCH[CPU]
    assert receipt["variant_expected"] == "linux-cpu" and receipt["pip_check"] == "ok"


def test_non_governed_lock_blocks():
    probs, receipt = ds.evaluate("locks/dev.txt", **_kwargs(CPU))
    assert receipt == {} and any("no gobernado" in p for p in probs)


def test_wrong_platform_blocks():
    probs, _ = ds.evaluate(CPU, **_kwargs(CPU, system="Darwin", machine="arm64"))
    assert any("plataforma" in p for p in probs)


def test_wrong_torch_blocks():
    probs, _ = ds.evaluate(CPU, **_kwargs(CPU, torch_version="2.12.0+cpu"))
    assert any("torch" in p for p in probs)


def test_wrong_dist_version_blocks():
    inst = _installed(CPU)
    inst["mlflow"] = "9.9.9"  # una distribución DEL contrato con versión que no casa el pin del lock
    probs, receipt = ds.evaluate(CPU, **_kwargs(CPU, installed=inst))
    assert receipt == {} and any("mlflow" in p for p in probs)


def test_b322_missing_inventory_component_blocks():
    # B322: omitir CUALQUIER componente del contrato ⇒ problema + recibo vacío (antes se emitía recibo verde sin ray).
    inst = _installed(CPU)
    del inst["ray"]
    probs, receipt = ds.evaluate(CPU, **_kwargs(CPU, installed=inst))
    assert receipt == {} and any("OMITE" in p and "ray" in p for p in probs)


def test_b322_extra_inventory_component_blocks():
    # B322: una distribución EXTRA fuera del contrato ⇒ problema + recibo vacío.
    inst = _installed(CPU)
    inst["evil"] = "0.0.0"
    probs, receipt = ds.evaluate(CPU, **_kwargs(CPU, installed=inst))
    assert receipt == {} and any("EXTRA" in p and "evil" in p for p in probs)


def test_b322_invalid_inventory_type_blocks():
    # tipos exactos: un inventario que no es dict[str, str] ⇒ problema + recibo vacío.
    probs, receipt = ds.evaluate(CPU, **_kwargs(CPU, installed={"ray": 2}))
    assert receipt == {} and any("inv" in p.lower() for p in probs)


def test_pip_check_red_blocks():
    probs, _ = ds.evaluate(CPU, **_kwargs(CPU, pip_check_ok=False))
    assert any("pip check" in p for p in probs)


def test_wrong_checksum_blocks():
    probs, _ = ds.evaluate(CPU, **_kwargs(CPU, checksum=55.0))
    assert any("checksum" in p for p in probs)


def test_contract_red_blocks(monkeypatch):
    monkeypatch.setattr(ds.lc, "validate_all", lambda root: ["manifiesto roto"])
    probs, _ = ds.evaluate(CPU, **_kwargs(CPU))
    assert any("contrato" in p for p in probs)


def test_all_governed_locks_have_runtime():
    # los 3 locks deep del contrato tienen su expectativa de ejecución
    assert set(lc.DEEP_RUNTIME) == set(lc.DEEP_LOCKS)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
