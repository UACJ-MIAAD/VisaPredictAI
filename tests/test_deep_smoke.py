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
        commit_sha="a" * 40,  # B328: commit real de 40-hex (el verificado lo calcula observe_runtime)
    )
    base.update(over)
    return base


def _observation(lock_rel, **over):
    """`DeepObservation` sintético (inventario == contrato canónico + pines del lock) para probar la construcción del recibo
    sin el stack deep instalado, monkeypatcheando `observe_runtime`. NO es un bypass: la API pública (`certify_runtime`) no
    acepta observación del caller (B335); esto sólo ejercita la ruta feliz de la construcción del recibo."""
    rt = lc.DEEP_RUNTIME[lock_rel]
    installed = _installed(lock_rel)
    base = dict(
        py_version="3.14.2",
        system=rt["system"],
        machine=rt["machine"],
        installed=tuple(sorted(installed.items())),
        torch_version=rt["torch"],
        pip_check_ok=True,
        checksum=83.0,
        commit_sha="a" * 40,
        import_records=tuple((m, d, f"lib/x/{m}/__init__.py", "sha256:" + "0" * 64) for m, d in CONTRACT.imports),
        identity_problems=(),
    )
    base.update(over)
    return ds.DeepObservation(**base)


def test_happy_path_receipt_is_lock_and_contract_bound(monkeypatch):
    # `certify_runtime` OBSERVA por su cuenta (B335); se monkeypatchea `observe_runtime` con una observación sintética para
    # ejercitar la construcción del recibo sin el stack deep.
    monkeypatch.setattr(ds, "observe_runtime", lambda lock_rel: ([], _observation(lock_rel)))
    probs, receipt = ds.certify_runtime(CPU)
    assert probs == []
    assert receipt["lock_sha256"].startswith("sha256:") and len(receipt["lock_sha256"]) == 71
    assert receipt["manifest_sha256"].startswith("sha256:")
    assert receipt["deep_smoke_contract_sha256"] == CONTRACT.sha256  # B322: recibo LIGADO al contrato de inventario
    assert list(receipt["versions"]) == CONTRACT_DISTS  # orden CANÓNICO del contrato
    assert receipt["commit_sha"] and receipt["torch_observed"] == lc.DEEP_TORCH[CPU]
    assert receipt["variant_expected"] == "linux-cpu" and receipt["pip_check"] == "ok"
    assert receipt["imports"][0].keys() == {"module", "distribution", "origin", "origin_sha256"}  # B332


def test_b331_reduced_contract_never_certifies():
    # B331: en el SHA base `evaluate()` construía un recibo con el contrato del CALLER — un `for_test((('torch','torch'),))`
    # + `installed={'torch': …}` producía recibo verde reduciendo la autoridad a torch. Ahora `evaluate()` es PURO de
    # problemas y JAMÁS devuelve recibo (siempre `{}`); la certificación vive sólo en `certify_runtime`, que observa por su
    # cuenta y recarga el contrato canónico. Un contrato reducido no puede certificar por ninguna vía pública.
    rt = lc.DEEP_RUNTIME[CPU]
    reduced = ds.DeepSmokeContract.for_test((("torch", "torch"),))
    probs, receipt = ds.evaluate(
        CPU,
        py_version="3.14.2",
        system=rt["system"],
        machine=rt["machine"],
        installed={"torch": rt["torch"]},
        torch_version=rt["torch"],
        pip_check_ok=True,
        checksum=83.0,
        contract=reduced,
        commit_sha="a" * 40,
    )
    assert receipt == {}, "un contrato reducido a torch JAMÁS debe certificar (B331)"
    assert probs == []  # los checks pasan, pero evaluate no emite recibo


def test_b335_no_public_api_accepts_a_caller_observation():
    # B335: en el SHA base `certify_observation(lock_rel, observation)` aceptaba un `DeepObservation` construible por el
    # caller (inventario/orígenes/commit fabricados) → recibo verde. Ahora NO existe tal API: el ÚNICO emisor
    # `certify_runtime` acepta SÓLO `lock_rel` y observa por su cuenta.
    import inspect

    assert not hasattr(ds, "certify_observation"), "certify_observation debe desaparecer (B335)"
    assert set(inspect.signature(ds.certify_runtime).parameters) == {"lock_rel"}, inspect.signature(ds.certify_runtime)


def test_b335_import_records_cross_checked_with_contract():
    # B335: antes de emitir recibo, `certify_runtime` cruza los import_records con el contrato — longitud/orden/module/dist/
    # origen relativo simple/`origin_sha256` canónico. Un registro degradado BLOQUEA el recibo.
    m0, d0 = CONTRACT.imports[0]
    good = tuple((m, d, f"lib/x/{m}/__init__.py", "sha256:" + "0" * 64) for m, d in CONTRACT.imports)
    assert ds._import_records_problems(good, CONTRACT) == []
    for bad, needle in [
        ((m0, d0, "/abs/x.py", "sha256:" + "0" * 64), "relativa simple"),
        ((m0, d0, "../up.py", "sha256:" + "0" * 64), "relativa simple"),
        ((m0, d0, "unknown", "sha256:" + "0" * 64), "relativa simple"),
        ((m0, d0, f"lib/x/{m0}/__init__.py", "notasha"), "origin_sha256"),
        (("evil", d0, f"lib/x/{m0}/__init__.py", "sha256:" + "0" * 64), "!= contrato"),
    ]:
        recs = (bad, *good[1:])
        assert any(needle in p for p in ds._import_records_problems(recs, CONTRACT)), bad
    assert any("import_records" in p for p in ds._import_records_problems(good[:-1], CONTRACT))


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


def test_b327_evaluate_surfaces_import_identity_problems():
    # los problemas de identidad calculados en observe_runtime() se propagan a evaluate() (recibo vacío). La identidad por
    # DESCRIPTOR (B332) vive en tests/test_governed_import_identity.py.
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
