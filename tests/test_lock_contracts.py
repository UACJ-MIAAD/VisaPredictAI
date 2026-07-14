"""Adversarial del contrato estático único de locks (tools/lock_contracts.py, P0R.4R).

Construye un repo FALSO contract-válido y muta un aspecto por caso: cada mutación DEBE producir al
menos un problema. Cubre los casos que el auditor exige (hash de manifiesto/fuente, lock ausente/
adicional, conteo de pins, toolchain/Python, pin duplicado, pin transitivo sin hash, wrapper
alterado, índice incorrecto, ruta temporal, divergencia de versión entre plataformas).
"""

from __future__ import annotations

import hashlib
import json

import pytest

import tools.lock_contracts as lc

_H = "sha256:" + "a" * 64
_GEN = {
    "python": "3.14.2",
    "platform": "Darwin arm64",
    "pip": "26.1.2",
    "setuptools": "83.0.0",
    "wheel": "0.47.0",
    "uv": "0.11.28",
}


def _sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _lock_text(rel: str, hashed: bool) -> str:
    if rel in lc.DEEP_LOCKS:
        lines = list(lc.DEEP_INDEX[rel]) + [""]
        for pkg, ver in {**lc.DEEP_DIRECT, "torch": lc.DEEP_TORCH[rel]}.items():
            lines.append(f"{pkg}=={ver} \\")
            lines.append(f"    --hash={_H}")
        return "\n".join(lines) + "\n"
    # locks base: 2 pins (con hash si el perfil es hasheado)
    lines = []
    for i, pkg in enumerate(("alpha", "beta")):
        lines.append(f"{pkg}=={1 + i}.0.0" + (" \\" if hashed else ""))
        if hashed:
            lines.append(f"    --hash={_H}")
    return "\n".join(lines) + "\n"


def _build_manifest(root) -> dict:
    return {
        "schema_version": 1,
        "generator": dict(_GEN),
        "sources": {s: _sha((root / s).read_bytes()) for s in lc.SOURCES},
        "locks": {
            f"locks/{n}": {
                "sha256": _sha((root / "locks" / n).read_bytes()),
                "pins": len(lc.pin_map((root / "locks" / n).read_text())),
            }
            for n in lc.LOCK_NAMES
        },
    }


@pytest.fixture
def repo(tmp_path):
    """Repo FALSO contract-válido: validate_all(root) == []."""
    root = tmp_path / "repo"
    (root / "locks").mkdir(parents=True)
    (root / "requirements").mkdir(parents=True)
    (root / "tools").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")
    (root / "tools/make_locks.sh").write_text("# stub generador\n")
    (root / "tools/promote_lockset.py").write_text("# stub promotor\n")
    (root / "tools/lock_contracts.py").write_text("# stub contrato\n")
    (root / "requirements/deep.in").write_text(
        "\n".join(f"{p}=={v}" for p, v in {**lc.DEEP_DIRECT, "torch": lc.TORCH_PUBLIC}.items()) + "\n"
    )
    for w, lines in lc.WRAPPERS.items():
        (root / w).write_text("\n".join(lines) + "\n")
    for rel, _profile, hashed in lc.LOCK_SPECS:
        (root / rel).write_text(_lock_text(rel, hashed))
    (root / "locks/lockset.json").write_text(json.dumps(_build_manifest(root), indent=2, sort_keys=True) + "\n")
    return root


def test_valid_repo_passes(repo):
    assert lc.validate_all(repo) == []


def _remanifest(repo):
    (repo / "locks/lockset.json").write_text(json.dumps(_build_manifest(repo), indent=2, sort_keys=True) + "\n")


def test_dup_pin_blocks(repo):
    # NO se re-manifesta: pin_map ya rechaza el dup (validate_files lo caza directo)
    p = repo / "locks/runtime.txt"
    p.write_text(p.read_text() + "alpha==9.9.9\n")
    assert any("duplicado" in x for x in lc.validate_all(repo))


def test_transitive_pin_without_hash_blocks(repo):
    # dev-linux es hasheado: añade un pin SIN hash
    p = repo / "locks/dev-linux-x86_64.txt"
    p.write_text(p.read_text() + "gamma==1.0.0\n")
    _remanifest(repo)
    assert any("sin hash" in x for x in lc.validate_all(repo))


def test_temp_path_blocks(repo):
    p = repo / "locks/model-cpu.txt"
    p.write_text(p.read_text() + "# via -c /var/folders/xx/staging\n")
    _remanifest(repo)
    assert any("ruta temporal" in x for x in lc.validate_all(repo))


def test_wrong_deep_index_blocks(repo):
    p = repo / "locks/deep-linux-x86_64-cpu.txt"
    p.write_text(p.read_text().replace("whl/cpu", "whl/cu999"))
    _remanifest(repo)
    assert any("índices" in x for x in lc.validate_all(repo))


def test_unknown_option_blocks(repo):
    # NO se re-manifesta: pin_map ya rechaza la opción desconocida
    p = repo / "locks/runtime.txt"
    p.write_text("--find-links https://evil.example\n" + p.read_text())
    assert any("opción desconocida" in x for x in lc.validate_all(repo))


def test_altered_wrapper_blocks(repo):
    (repo / "requirements/deep-linux-cpu.in").write_text("--extra-index-url https://evil/whl/cpu\n-r deep.in\n")
    _remanifest(repo)
    assert any("wrapper" in x for x in lc.validate_all(repo))


def test_wrong_deep_direct_version_blocks(repo):
    p = repo / "locks/deep-macos-arm64.txt"
    p.write_text(p.read_text().replace("pandas==2.3.3", "pandas==3.0.0"))
    _remanifest(repo)
    assert any("pandas" in x for x in lc.validate_all(repo))


def test_cross_platform_public_divergence_blocks(repo):
    # numpy diverge SOLO en un lock deep (misma etiqueta, versión pública distinta)
    p = repo / "locks/deep-linux-x86_64-cpu.txt"
    p.write_text(p.read_text().replace("numpy==2.4.6", "numpy==2.4.5"))
    _remanifest(repo)
    assert any("divergencia de versión pública de numpy" in x for x in lc.validate_all(repo))


def test_manifest_bad_lock_hash_blocks(repo):
    m = json.loads((repo / "locks/lockset.json").read_text())
    m["locks"]["locks/runtime.txt"]["sha256"] = "sha256:" + "0" * 64
    (repo / "locks/lockset.json").write_text(json.dumps(m, indent=2, sort_keys=True) + "\n")
    assert any("sha256 != real" in x for x in lc.validate_all(repo))


def test_manifest_bad_source_hash_blocks(repo):
    m = json.loads((repo / "locks/lockset.json").read_text())
    m["sources"]["tools/make_locks.sh"] = "sha256:" + "0" * 64
    (repo / "locks/lockset.json").write_text(json.dumps(m, indent=2, sort_keys=True) + "\n")
    assert any("sources[tools/make_locks.sh]" in x for x in lc.validate_all(repo))


def test_manifest_missing_source_blocks(repo):
    # sin lock_contracts.py como fuente gobernada -> claves != las 7
    m = json.loads((repo / "locks/lockset.json").read_text())
    del m["sources"]["tools/lock_contracts.py"]
    (repo / "locks/lockset.json").write_text(json.dumps(m, indent=2, sort_keys=True) + "\n")
    assert any("manifiesto.sources" in x for x in lc.validate_all(repo))


def test_manifest_extra_or_missing_lock_blocks(repo):
    m = json.loads((repo / "locks/lockset.json").read_text())
    del m["locks"]["locks/dev.txt"]
    (repo / "locks/lockset.json").write_text(json.dumps(m, indent=2, sort_keys=True) + "\n")
    assert any("manifiesto.locks" in x for x in lc.validate_all(repo))


def test_manifest_wrong_pin_count_blocks(repo):
    m = json.loads((repo / "locks/lockset.json").read_text())
    m["locks"]["locks/runtime.txt"]["pins"] = 99
    (repo / "locks/lockset.json").write_text(json.dumps(m, indent=2, sort_keys=True) + "\n")
    assert any("pins 99" in x for x in lc.validate_all(repo))


def test_manifest_wrong_toolchain_blocks(repo):
    m = json.loads((repo / "locks/lockset.json").read_text())
    m["generator"]["pip"] = "1.2.3"
    (repo / "locks/lockset.json").write_text(json.dumps(m, indent=2, sort_keys=True) + "\n")
    assert any("generator.pip" in x for x in lc.validate_all(repo))


def test_manifest_wrong_python_blocks(repo):
    m = json.loads((repo / "locks/lockset.json").read_text())
    m["generator"]["python"] = "3.13.1"  # minor incorrecto
    (repo / "locks/lockset.json").write_text(json.dumps(m, indent=2, sort_keys=True) + "\n")
    assert any("python" in x and "3.14" in x for x in lc.validate_all(repo))


def test_manifest_python_not_full_blocks(repo):
    m = json.loads((repo / "locks/lockset.json").read_text())
    m["generator"]["python"] = "3.14"  # sin patch
    (repo / "locks/lockset.json").write_text(json.dumps(m, indent=2, sort_keys=True) + "\n")
    assert any("no es 3.14.Z" in x for x in lc.validate_all(repo))


def test_missing_manifest_blocks(repo):
    (repo / "locks/lockset.json").unlink()
    assert any("manifiesto ausente" in x for x in lc.validate_all(repo))


def test_manifest_wrong_platform_blocks(repo):
    m = json.loads((repo / "locks/lockset.json").read_text())
    m["generator"]["platform"] = "Linux x86_64"
    (repo / "locks/lockset.json").write_text(json.dumps(m, indent=2, sort_keys=True) + "\n")
    assert any("platform" in x for x in lc.validate_all(repo))


def test_json_duplicate_key_blocks(repo):
    # manifiesto con clave duplicada (json.dumps no lo produce; se escribe crudo)
    (repo / "locks/lockset.json").write_text('{"schema_version": 1, "schema_version": 1}\n')
    assert any("duplicada" in x for x in lc.validate_all(repo))


def test_extra_txt_in_locks_blocks(repo):
    (repo / "locks/extra.txt").write_text("junk==1.0.0\n")
    assert any("conjunto de .txt" in x for x in lc.validate_all(repo))


def test_symlink_lock_blocks(repo, tmp_path):
    target = tmp_path / "outside.txt"
    target.write_text((repo / "locks/runtime.txt").read_text())
    (repo / "locks/runtime.txt").unlink()
    (repo / "locks/runtime.txt").symlink_to(target)
    assert any("symlink" in x or "fuera de la raíz" in x for x in lc.validate_all(repo))


def test_deep_in_extra_pin_blocks(repo):
    p = repo / "requirements/deep.in"
    p.write_text(p.read_text() + "extrapkg==9.9.9\n")
    _remanifest(repo)
    assert any("requirements/deep.in" in x and "conjunto gobernado" in x for x in lc.validate_all(repo))


def test_strict_hash_format_blocks(repo):
    p = repo / "locks/deep-macos-arm64.txt"
    p.write_text(p.read_text().replace(_H, "sha256:notavalidhash", 1))
    _remanifest(repo)
    assert any("hash no es sha256" in x for x in lc.validate_all(repo))


def test_validate_generator_unit():
    good = dict(_GEN)
    assert lc.validate_generator(good) == []
    assert any("python" in x for x in lc.validate_generator({**good, "python": "3.14"}))
    assert any("python" in x for x in lc.validate_generator({**good, "python": "3.13.2"}))
    assert any("platform" in x for x in lc.validate_generator({**good, "platform": "Linux x86_64"}))
    assert any("pip" in x for x in lc.validate_generator({**good, "pip": "1.0.0"}))
    assert lc.validate_generator({"python": "3.14.2"}) != []  # claves faltantes


def test_real_repo_contract_holds():
    # el repo REAL debe cumplir el contrato completo (locks + manifiesto)
    assert lc.validate_all(lc.ROOT) == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
