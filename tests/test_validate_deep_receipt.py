"""B328: validador INDEPENDIENTE del recibo de deep smoke — re-deriva todo y no confía en el auto-reporte.

Núcleo PURO `receipt_problems` con re-derivados inyectados + lectura fd-bound. Cubre: esquema exacto, commit 40-hex ==
HEAD y == GITHUB_SHA, Python fullmatch, plataforma/variante, hashes de lock/manifiesto/contrato, versiones == pins,
orígenes relativos (sin `..`/absoluta), pip_check/checksum, y rechazo de symlink/duplicados en la lectura.
"""

from __future__ import annotations

import json

import pytest

import tools.deep_smoke as ds
import tools.lock_contracts as lc
import tools.validate_deep_receipt as v

CPU = "locks/deep-linux-x86_64-cpu.txt"
CONTRACT = ds.load_contract()
HEAD = "a" * 40


def _rt():
    return lc.DEEP_RUNTIME[CPU]


def _good():
    rt = _rt()
    pins = lc.pin_map((lc.ROOT / CPU).read_text())
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
        "versions": {d: (rt["torch"] if d == "torch" else pins[lc._norm(d)]) for _, d in CONTRACT.imports},
        "imports": [
            {"module": m, "distribution": d, "origin": f"lib/site-packages/{m}/__init__.py"}
            for m, d in CONTRACT.imports
        ],  # fmt: skip
        "tensor_checksum": 83.0,
    }


def _kw(**over):
    base = dict(
        lock_rel=CPU,
        expected_variant="linux-cpu",
        contract=CONTRACT,
        lock_sha=ds._sha256(lc.ROOT / CPU),
        manifest_sha=ds._sha256(lc.ROOT / lc.MANIFEST_REL),
        git_head=HEAD,
        github_sha=HEAD,
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
    }
    for label, (over, needle) in cases.items():
        r = {**_good(), **over}
        probs = v.receipt_problems(r, **_kw())
        assert any(needle in p for p in probs), f"{label}: {probs}"


def test_b328_github_sha_must_match_head():
    probs = v.receipt_problems(_good(), **_kw(github_sha="c" * 40))
    assert any("GITHUB_SHA" in p for p in probs)


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


def test_b328_read_rejects_symlink(tmp_path):
    real = tmp_path / "real.json"
    real.write_text(json.dumps(_good()))
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(OSError):  # O_NOFOLLOW
        v._read_receipt_governed(str(link))


def test_b328_read_rejects_duplicate_keys(tmp_path):
    p = tmp_path / "dup.json"
    p.write_text('{"a": 1, "a": 2}')
    with pytest.raises(ValueError, match="duplicada"):
        v._read_receipt_governed(str(p))


def test_b328_producer_governed_write(tmp_path, monkeypatch):
    # el productor escribe con O_EXCL|O_NOFOLLOW 0600: no sobrescribe, no sigue symlink, no acepta ruta absoluta/`..`.
    monkeypatch.chdir(tmp_path)  # auto-restaura el CWD (no contamina el resto de la suite)
    ds.write_receipt_governed("r.json", {"k": "v"})
    assert (tmp_path / "r.json").exists() and oct((tmp_path / "r.json").stat().st_mode)[-3:] == "600"
    with pytest.raises(FileExistsError):  # O_EXCL: no sobrescribe
        ds.write_receipt_governed("r.json", {"k": "v2"})
    for bad in ("/tmp/abs.json", "../escape.json"):
        with pytest.raises(ValueError, match="relativa simple"):
            ds.write_receipt_governed(bad, {"k": "v"})


def test_b328_validator_wired_in_ci_between_smoke_and_upload():
    # B328: el validador está CABLEADO en el job deep entre el smoke y el upload, sin `continue-on-error` ni `if`
    # condicional en su paso — un recibo verde con validador rojo aborta el job (no se sube).
    ci = (lc.ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "deep-lock-install" in ci
    job = ci.split("deep-lock-install", 1)[1].split("dvc-tool-install", 1)[0]
    smoke = job.index("python -m tools.deep_smoke")
    validate = job.index("python -m tools.validate_deep_receipt")
    upload = job.index("upload-artifact", smoke)
    assert smoke < validate < upload, "el validador debe ir ENTRE el smoke y el upload (B328)"
    step = job[job.rindex("- name:", 0, validate) : job.index("- ", validate + 1)]
    assert "continue-on-error" not in step and "\n        if:" not in step, "el paso del validador no puede omitirse (B328)"  # fmt: skip


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
