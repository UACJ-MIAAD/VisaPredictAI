"""B328/B333/B334: validador REALMENTE INDEPENDIENTE del recibo de deep smoke.

Núcleo PURO `receipt_problems` con re-derivados/reobservados inyectados. Cubre: esquema exacto, commit 40-hex == HEAD y ==
GITHUB_SHA, **HEAD ausente ⇒ ROJO (B333)**, Python/plataforma contra lo REOBSERVADO, hashes de lock/manifiesto/contrato,
versiones == pins Y == reobservado, orígenes relativos + `origin_sha256` == reobservado (B332), pip_check/checksum, IO
gobernado del recibo (nombre simple, sin ancestro symlink — B334) y el cableado CI con paso negativo obligatorio."""

from __future__ import annotations

import json
import os

import pytest

import tools.deep_smoke as ds
import tools.governed_receipt_io as grio
import tools.lock_contracts as lc
import tools.validate_deep_receipt as v

CPU = "locks/deep-linux-x86_64-cpu.txt"
CONTRACT = ds.load_contract()
HEAD = "a" * 40
# fuente única: (module, distribution, origin, origin_sha256) — recibo y `observed` coinciden por construcción.
IMPORTS = [(m, d, f"lib/site-packages/{m}/__init__.py", "sha256:" + "1" * 64) for m, d in CONTRACT.imports]


def _rt():
    return lc.DEEP_RUNTIME[CPU]


def _versions():
    rt = _rt()
    pins = lc.pin_map((lc.ROOT / CPU).read_text())
    return {d: (rt["torch"] if d == "torch" else pins[lc._norm(d)]) for _, d in CONTRACT.imports}


def _good():
    rt = _rt()
    return {
        "commit_sha": HEAD,
        "lock": CPU,
        "lock_sha256": ds._sha256(lc.ROOT / CPU),
        "manifest_sha256": ds._sha256(lc.ROOT / lc.MANIFEST_REL),
        "deep_smoke_contract_sha256": CONTRACT.sha256,
        "variant_expected": "linux-cpu",
        "platform_expected": f"{rt['system']} {rt['machine']}",
        "platform_observed": f"{rt['system']} {rt['machine']}",
        "python": "3.14.2",
        "torch_expected": rt["torch"],
        "torch_observed": rt["torch"],
        "pip_check": "ok",
        "versions": _versions(),
        "imports": [{"module": m, "distribution": d, "origin": o, "origin_sha256": s} for m, d, o, s in IMPORTS],
        "tensor_checksum": 83.0,
    }


def _observed():
    ver = _versions()
    return {m: {"distribution": d, "version": ver[d], "origin": o, "origin_sha256": s} for m, d, o, s in IMPORTS}


def _kw(**over):
    rt = _rt()
    base = dict(
        lock_rel=CPU,
        expected_variant="linux-cpu",
        contract=CONTRACT,
        lock_sha=ds._sha256(lc.ROOT / CPU),
        manifest_sha=ds._sha256(lc.ROOT / lc.MANIFEST_REL),
        pins=lc.pin_map((lc.ROOT / CPU).read_text()),
        git_head=HEAD,
        github_sha=HEAD,
        real_python="3.14.2",
        real_system=rt["system"],
        real_machine=rt["machine"],
        observed=_observed(),
    )
    base.update(over)
    return base


def test_good_receipt_validates():
    assert v.receipt_problems(_good(), **_kw()) == []


def test_b328_rejects_forged_provenance_and_schema():
    cases = {
        "not_a_commit": ({"commit_sha": "NOT-A-COMMIT"}, "40-hex"),
        "commit_ne_head": ({"commit_sha": "b" * 40}, "!= HEAD"),
        "python_evil": ({"python": "3.14.evil"}, "3.14.Z"),
        "wrong_variant": ({"variant_expected": "linux-gpu"}, "variante"),
        "forged_lock_sha": ({"lock_sha256": "sha256:dead"}, "lock_sha256"),
        "forged_contract_sha": ({"deep_smoke_contract_sha256": "sha256:dead"}, "contrato"),
        "pip_red": ({"pip_check": "fail"}, "pip_check"),
        "bad_checksum": ({"tensor_checksum": 55.0}, "tensor_checksum"),
        "bool_checksum": ({"tensor_checksum": True}, "tensor_checksum"),
    }
    for label, (over, needle) in cases.items():
        probs = v.receipt_problems({**_good(), **over}, **_kw())
        assert any(needle in p for p in probs), f"{label}: {probs}"


def test_b333_missing_git_head_is_red():
    # B333: en el SHA base, con git_head=None el commit/Python/orígenes fabricados se ACEPTABAN (fail-open). Ahora un HEAD
    # no resuelto es SIEMPRE un problema.
    probs = v.receipt_problems(_good(), **_kw(git_head=None))
    assert any("HEAD no resuelto" in p and "fail-closed" in p for p in probs), probs


def test_b333_python_and_platform_vs_reobserved():
    # el recibo debe coincidir con lo REOBSERVADO, no sólo con el patrón/expectativa.
    assert any("!= reobservado" in p for p in v.receipt_problems(_good(), **_kw(real_python="3.14.9")))
    tampered = {**_good(), "platform_observed": "Linux evilarch"}
    assert any("platform_observed" in p for p in v.receipt_problems(tampered, **_kw()))


def test_b328_github_sha_must_match_head():
    assert any("GITHUB_SHA" in p for p in v.receipt_problems(_good(), **_kw(github_sha="c" * 40)))


def test_b328_exact_schema_no_extra_no_missing():
    assert any("esquema exacto" in p for p in v.receipt_problems({**_good(), "evil": 1}, **_kw()))
    r = _good()
    del r["imports"]
    assert any("esquema exacto" in p for p in v.receipt_problems(r, **_kw()))


def test_b328_version_and_origin_tamper():
    r = _good()
    r["versions"] = {**r["versions"], "mlflow": "9.9.9"}
    assert any("mlflow" in p for p in v.receipt_problems(r, **_kw()))
    for bad_origin in ("/etc/passwd", "../escape/x.py", "unknown"):
        r = _good()
        r["imports"] = [{**r["imports"][0], "origin": bad_origin}, *r["imports"][1:]]
        assert any("origin" in p for p in v.receipt_problems(r, **_kw())), bad_origin


def test_b332_reobserved_origin_sha_and_version_tamper():
    # §10.6: el recibo se cruza contra la identidad/versión REOBSERVADAS por descriptor.
    r = _good()
    r["imports"] = [{**r["imports"][0], "origin_sha256": "sha256:" + "e" * 64}, *r["imports"][1:]]
    assert any("origin_sha256" in p for p in v.receipt_problems(r, **_kw()))
    first = IMPORTS[0][0]  # module con versión reobservada distinta
    obs = {**_observed(), first: {**_observed()[first], "version": "0.0.0-evil"}}
    assert any("reobservada" in p for p in v.receipt_problems(_good(), **_kw(observed=obs)))


def test_b332_import_schema_requires_origin_sha256():
    r = _good()
    r["imports"] = [
        {"module": IMPORTS[0][0], "distribution": IMPORTS[0][1], "origin": IMPORTS[0][2]},
        *r["imports"][1:],
    ]
    assert any("origin_sha256" in p or "module" in p for p in v.receipt_problems(r, **_kw()))


def test_b334_read_rejects_nonsimple_names():
    for bad in ("a/b.json", "/abs.json", "../up.json", ".", "..", ""):
        with pytest.raises(ValueError, match="nombre de recibo"):
            grio.read_receipt_bytes(bad)
        with pytest.raises(ValueError, match="nombre de recibo"):
            grio.write_receipt(bad, {"k": "v"})


def test_b334_read_and_write_reject_leaf_symlink(tmp_path):
    (tmp_path / "real.json").write_text(json.dumps({"x": 1}))
    os.symlink(str(tmp_path / "real.json"), str(tmp_path / "link.json"))
    with pytest.raises(OSError):  # O_NOFOLLOW en el leaf relativo al fd de directorio
        grio.read_receipt_bytes("link.json", authorized_dir=str(tmp_path))
    # el ancestro (authorized_dir) symlink tampoco se sigue: O_DIRECTORY|O_NOFOLLOW
    os.symlink(str(tmp_path), str(tmp_path / "dirlink"))
    with pytest.raises(OSError):
        grio.read_receipt_bytes("real.json", authorized_dir=str(tmp_path / "dirlink"))


def test_b334_read_rejects_duplicate_keys(tmp_path):
    (tmp_path / "dup.json").write_text('{"a": 1, "a": 2}')
    raw = grio.read_receipt_bytes("dup.json", authorized_dir=str(tmp_path))
    with pytest.raises(ValueError, match="duplicada"):
        json.loads(raw.decode("utf-8"), object_pairs_hook=ds._no_dup_keys)


def test_b334_governed_write_round_trip_and_o_excl(tmp_path, monkeypatch):
    # nombre SIMPLE en el directorio autorizado (CWD), 0600, O_EXCL (no sobrescribe), sin symlink; nombres no simples fallan.
    monkeypatch.chdir(tmp_path)
    ds.write_receipt_governed("r.json", {"k": "v"})
    assert (tmp_path / "r.json").exists() and oct((tmp_path / "r.json").stat().st_mode)[-3:] == "600"
    assert json.loads(grio.read_receipt_bytes("r.json")) == {"k": "v"}
    with pytest.raises(FileExistsError):  # O_EXCL: no sobrescribe
        ds.write_receipt_governed("r.json", {"k": "v2"})
    for bad in ("/tmp/abs.json", "../escape.json", "sub/r.json"):
        with pytest.raises(ValueError, match="nombre de recibo"):
            ds.write_receipt_governed(bad, {"k": "v"})


def test_wired_in_ci_with_negative_test_between_smoke_and_upload():
    # B328/B333: smoke → validador → NEGATIVO obligatorio (recibo manipulado debe fallar) → upload, sin `if`/
    # `continue-on-error` en los pasos del validador ni del negativo.
    ci = (lc.ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "deep-lock-install" in ci
    job = ci.split("deep-lock-install", 1)[1].split("dvc-tool-install", 1)[0]
    smoke = job.index("python -m tools.deep_smoke")
    validate = job.index("python -m tools.validate_deep_receipt")
    negative = job.index("recibo manipulado DEBE fallar")
    upload = job.index("upload-artifact", smoke)
    assert smoke < validate < negative < upload, "orden smoke < validador < negativo < upload"
    for anchor in (validate, negative):
        step = job[job.rindex("- name:", 0, anchor) : job.index("- ", anchor + 1)]
        assert "continue-on-error" not in step and "\n        if:" not in step, "el paso no puede omitirse"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
