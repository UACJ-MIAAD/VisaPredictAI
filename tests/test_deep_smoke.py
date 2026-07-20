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
CONTRACT = ds.load_contract()  # autoridad INDEPENDIENTE del inventario, DeepSmokeContract inmutable (B323/B326)
CONTRACT_DISTS = [d for _, d in CONTRACT.imports]


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
        contract=CONTRACT,
        commit_sha="a" * 40,  # B328: commit real de 40-hex (el verificado lo calcula run())
    )
    base.update(over)
    return base


def test_happy_path_receipt_is_lock_and_contract_bound():
    probs, receipt = ds.evaluate(CPU, **_kwargs(CPU))
    assert probs == []
    assert receipt["lock_sha256"].startswith("sha256:") and len(receipt["lock_sha256"]) == 71
    assert receipt["manifest_sha256"].startswith("sha256:")
    assert receipt["deep_smoke_contract_sha256"] == CONTRACT.sha256  # B322: recibo LIGADO al contrato de inventario
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


def test_b326_forged_contract_is_rejected():
    # B326: un caller NO puede forjar el `DeepSmokeContract` — el sha debe coincidir con `canonical_bytes` y los imports
    # deben re-parsear IGUAL (contenido↔hash↔imports cruzados). Antes `evaluate()` aceptaba lista+sha sueltos.
    with pytest.raises(ValueError, match="sha256 no coincide"):
        ds.DeepSmokeContract(imports=(), canonical_bytes=b"", sha256="FORGED")
    real = ds.load_contract()
    with pytest.raises(ValueError, match="imports no coincide"):  # sha real, pero imports mentidos
        ds.DeepSmokeContract(imports=(("evil", "evil"),), canonical_bytes=real.canonical_bytes, sha256=real.sha256)


def test_b326_evaluate_rejects_non_contract():
    # B326: `evaluate()` exige `type(x) is DeepSmokeContract` — ni una lista/tupla+sha sueltos ni una subclase.
    for forged in ([], (), CONTRACT_DISTS, "sha256:deadbeef"):
        probs, receipt = ds.evaluate(CPU, **{**_kwargs(CPU), "contract": forged})
        assert receipt == {} and any("contrato deep inválido" in p for p in probs), (forged, probs)

    class _Sub(ds.DeepSmokeContract):
        pass

    sub = _Sub(imports=CONTRACT.imports, canonical_bytes=CONTRACT.canonical_bytes, sha256=CONTRACT.sha256)
    probs, receipt = ds.evaluate(CPU, **{**_kwargs(CPU), "contract": sub})
    assert receipt == {} and any("contrato deep inválido" in p for p in probs)


def test_b326_for_test_factory_revalidates():
    # `for_test` construye un contrato válido re-validando el mismo esquema; imports no canónicos son rechazados.
    good = ds.DeepSmokeContract.for_test((("a", "a"), ("b", "b")))
    assert good.imports == (("a", "a"), ("b", "b")) and good.sha256.startswith("sha256:")
    with pytest.raises(ValueError, match="orden canónico"):
        ds.DeepSmokeContract.for_test((("zzz", "zzz"), ("aaa", "aaa")))


PREFIX = "/opt/pyprefix"
REPO = "/repo/root"


def test_b327_identity_accepts_real_origin():
    # módulo con origin REAL bajo sys.prefix y provisto por EXACTAMENTE la distribución esperada → sin problemas.
    assert (
        ds.identity_problems(
            "ray",
            "ray",
            origin=PREFIX + "/lib/site-packages/ray/__init__.py",
            providing=["ray"],
            sys_prefix=PREFIX,
            root=REPO,
        )
        == []
    )


def test_b327_identity_rejects_fake_and_shadow_and_wrong_dist():
    # B327: un módulo preinyectado (sin origin), un shadow local bajo ROOT, un origin fuera de sys.prefix, o una
    # distribución que NO provee el módulo → problema. En el SHA base `run()` no cruzaba módulo↔distribución↔origen y
    # ocho módulos falsos en `sys.modules` producían recibo verde.
    assert any("sin origin" in p for p in ds.identity_problems("ray", "ray", origin=None, providing=["ray"], sys_prefix=PREFIX, root=REPO))  # fmt: skip
    shadow = ds.identity_problems("ray", "ray", origin=REPO + "/ray/__init__.py", providing=["ray"], sys_prefix=PREFIX, root=REPO)  # fmt: skip
    assert any("ROOT" in p for p in shadow) and any("fuera de sys.prefix" in p for p in shadow)
    assert any("fuera de sys.prefix" in p for p in ds.identity_problems("ray", "ray", origin="/tmp/ray/__init__.py", providing=["ray"], sys_prefix=PREFIX, root=REPO))  # fmt: skip
    assert any("packages_distributions" in p for p in ds.identity_problems("ray", "ray", origin=PREFIX + "/x/ray/__init__.py", providing=["evil"], sys_prefix=PREFIX, root=REPO))  # fmt: skip


def test_b327_evaluate_surfaces_import_identity_problems():
    # los problemas de identidad calculados en run() se propagan a evaluate() (recibo vacío).
    probs, receipt = ds.evaluate(CPU, **{**_kwargs(CPU), "import_identity": ["ray: sin origin certificable"]})
    assert receipt == {} and any("sin origin" in p for p in probs)


def test_b327_distribution_inventory_flags_duplicates(monkeypatch):
    # B327: dos dist-info con el MISMO nombre normalizado NO se sobrescriben en silencio (last-wins) — es un problema.
    class _D:
        def __init__(self, name, version):
            self.name = name
            self.version = version

    monkeypatch.setattr(ds, "distributions", lambda: [_D("Ray", "1.0"), _D("ray", "2.0"), _D("pandas", "3.0")])
    inv, probs = ds._distribution_inventory()
    assert any("DUPLICADO" in p and "ray" in p for p in probs) and inv["pandas"] == "3.0"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
