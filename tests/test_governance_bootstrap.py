"""B284/B290 REDs: instalador GOBERNADO de PyYAML (`tools/install_governance_bootstrap.py`), su validador independiente
(`tools/validate_governance_bootstrap.py`) y el registro POSITIVO de instalaciones de CI (`tools/check_ci_installs.py`).

Cubren los siete REDs de §7.2: (1) hash omitido/cambiado, (2) índice/config inyectado, (3) `-I` aislado + origen bajo
prefijo (yaml plantado no gana), (4) venv/ancestro escribible, (5) versión/origen falsificados, (6) recibo aceptado sin
re-observación, (7) `pip install` escondido en eval/heredoc/bash -c / install nuevo/obsoleto. Los end-to-end reales
(venv + pip --require-hashes) los ejercita CI; aquí se prueban las unidades fail-closed sin red."""

from __future__ import annotations

import json
import os

import pytest

import tools.check_ci_installs as ci
import tools.install_governance_bootstrap as boot
import tools.validate_governance_bootstrap as val

# --------------------------------------------------------------------------- RED 1: hash omitido/cambiado


def test_hashes_from_lock_requires_hashes():
    with pytest.raises(boot._BootstrapError):
        boot._hashes_from_lock(b"pyyaml==6.0.3\n", "6.0.3")  # el requerimiento no lleva --hash


def test_hashes_from_lock_missing_or_ambiguous_package():
    with pytest.raises(boot._BootstrapError):
        boot._hashes_from_lock(b"other==1.0 --hash=sha256:" + b"a" * 64 + b"\n", "6.0.3")


def test_hashes_from_lock_parses_and_sorts_continuations():
    lock = b"pyyaml==6.0.3 \\\n    --hash=sha256:" + b"b" * 64 + b" \\\n    --hash=sha256:" + b"a" * 64 + b"\n"
    assert boot._hashes_from_lock(lock, "6.0.3") == ["a" * 64, "b" * 64]


def test_pin_from_pyproject_exact_or_fail():
    assert boot._pin_from_pyproject(b'[project.optional-dependencies]\ndev = ["pyyaml==6.0.3"]\n') == "6.0.3"
    with pytest.raises(boot._BootstrapError):
        boot._pin_from_pyproject(b'[project.optional-dependencies]\ndev = ["ruff==1.0"]\n')  # sin pyyaml


def test_installer_uses_require_hashes_no_deps_isolated():
    # el instalador SIEMPRE instala con --no-deps --require-hashes y verifica bajo -I (regresión estructural)
    src = open(boot.__file__, encoding="utf-8").read()
    assert '"--no-deps", "--require-hashes"' in src
    assert '"-I", "-c", _VERIFY_SRC' in src


# --------------------------------------------------------------------------- RED 2: índice/config inyectado


def test_sanitized_env_drops_index_and_config(monkeypatch):
    for k in ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PYTHONPATH", "HOME", "GIT_CONFIG", "UV_INDEX_URL"):
        monkeypatch.setenv(k, "/evil")
    env = boot._sanitized_env()
    for k in ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PYTHONPATH", "HOME", "GIT_CONFIG", "UV_INDEX_URL"):
        assert k not in env, k
    assert env["PIP_NO_CACHE_DIR"] == "1" and env["PIP_DISABLE_PIP_VERSION_CHECK"] == "1"


# --------------------------------------------------------------------------- RED 3: -I aislado + origen bajo prefijo


def test_verify_requires_origin_under_prefix_regular_nonsymlink():
    for needle in ("yaml.__spec__.origin", "origin.startswith(prefix)", "os.path.islink(origin)", "stat.S_ISREG"):
        assert needle in boot._VERIFY_SRC, needle
        assert needle in val._VERIFY_SRC, needle


def test_runner_temp_rejects_inside_checkout_and_missing(monkeypatch):
    monkeypatch.delenv("RUNNER_TEMP", raising=False)
    with pytest.raises(boot._BootstrapError):
        boot._runner_temp()
    monkeypatch.setenv("RUNNER_TEMP", boot._ROOT)  # dentro del checkout
    with pytest.raises(boot._BootstrapError):
        boot._runner_temp()


# --------------------------------------------------------------------------- RED 4: venv/ancestro escribible


def test_validator_rejects_world_writable_ancestor(tmp_path):
    ww = tmp_path / "ww"
    ww.mkdir()
    os.chmod(ww, 0o777)  # world-writable SIN sticky
    venv = ww / "venv"
    venv.mkdir()
    os.chmod(venv, 0o700)
    assert val._ancestors_not_world_writable(str(venv)) is not None


def test_validator_requires_venv_0700(tmp_path):
    venv = tmp_path / "venv"
    venv.mkdir()
    os.chmod(venv, 0o755)  # no 0700
    assert val._ancestors_not_world_writable(str(venv)) is not None


# --------------------------------------------------------------------------- RED 5/6: recibo falsificado / re-observación


def test_validator_rejects_missing_receipt_keys(tmp_path, monkeypatch):
    venv = tmp_path / "venv"
    venv.mkdir()
    os.chmod(venv, 0o700)
    (venv / val._RECEIPT_NAME).write_text('{"schema_version": 1}')  # claves faltantes
    monkeypatch.setenv("GOV_ENV", str(venv))
    assert val.main() == 1


def test_validator_does_not_trust_receipt_alone(tmp_path, monkeypatch):
    # recibo con TODAS las claves pero shas/venv falsos → la re-observación gobernada (sha del lock) o la re-verificación
    # del venv (subprocess -I, sin python real) lo rechazan; el recibo por sí solo NO alcanza.
    venv = tmp_path / "venv"
    venv.mkdir()
    os.chmod(venv, 0o700)
    receipt = {k: "x" for k in val._RECEIPT_KEYS}
    receipt["venv_prefix"] = str(venv)
    (venv / val._RECEIPT_NAME).write_text(json.dumps(receipt))
    monkeypatch.setenv("GOV_ENV", str(venv))
    assert val.main() == 1


def test_validator_reobserves_via_isolated_subprocess():
    src = open(val.__file__, encoding="utf-8").read()
    assert 'subprocess.run([py, "-I", "-c", _VERIFY_SRC]' in src  # re-ejecuta la verificación, no confía en el recibo
    assert 'hashlib.sha256(lock).hexdigest() != receipt["lock_sha256"]' in src  # re-lee el lock gobernado


# --------------------------------------------------------------------------- RED 7: install escondido / nuevo / obsoleto


def _scan_synthetic(tmp_path, monkeypatch, wf_text):
    wfdir = tmp_path / ".github" / "workflows"
    wfdir.mkdir(parents=True)
    (wfdir / "x.yml").write_text(wf_text)
    monkeypatch.setattr(ci, "_ROOT", str(tmp_path))
    return ci._scan_workflows()


def test_ci_installs_flags_hidden_eval(tmp_path, monkeypatch):
    wf = 'jobs:\n  j:\n    steps:\n      - run: eval "pip install evil==1.0"\n'
    _obs, probs = _scan_synthetic(tmp_path, monkeypatch, wf)
    assert any("oculto" in p for p in probs), probs


def test_ci_installs_flags_hidden_heredoc(tmp_path, monkeypatch):
    wf = "jobs:\n  j:\n    steps:\n      - run: |\n          bash <<SH\n          pip install evil\n          SH\n"
    _obs, probs = _scan_synthetic(tmp_path, monkeypatch, wf)
    assert any("oculto" in p for p in probs), probs


def test_ci_installs_flags_hidden_bash_c(tmp_path, monkeypatch):
    wf = 'jobs:\n  j:\n    steps:\n      - run: bash -c "pip install evil"\n'
    _obs, probs = _scan_synthetic(tmp_path, monkeypatch, wf)
    assert any("oculto" in p for p in probs), probs


def test_ci_installs_biyection_detects_new_and_obsolete(monkeypatch):
    real = dict(ci._scan_workflows()[0])
    assert real, "debe haber instalaciones reales observadas"
    monkeypatch.setattr(ci, "_scan_workflows", lambda: ({**real, ("new.yml", "j", "pip install evil==1.0"): 1}, []))
    assert any("NO REGISTRADO" in p for p in ci.problems())
    trimmed = dict(real)
    trimmed.pop(next(iter(trimmed)))
    monkeypatch.setattr(ci, "_scan_workflows", lambda: (trimmed, []))
    assert any("OBSOLETO" in p for p in ci.problems())


def test_ci_installs_biyection_detects_count_divergence(monkeypatch):
    real = dict(ci._scan_workflows()[0])
    key = next(iter(real))
    bumped = {**real, key: real[key] + 1}
    monkeypatch.setattr(ci, "_scan_workflows", lambda: (bumped, []))
    assert any("conteo divergente" in p for p in ci.problems())


def test_ci_installs_registry_rejects_out_of_set_category_and_expired(monkeypatch):
    good = json.loads(open(os.path.join(ci._ROOT, ci._REGISTRY), encoding="utf-8").read())
    bad_cat = json.loads(json.dumps(good))
    bad_cat["installs"][0]["category"] = "made-up"
    monkeypatch.setattr(ci, "_load_registry", lambda: (bad_cat, []))
    assert any("fuera de" in p for p in ci.problems())
    expired = json.loads(json.dumps(good))
    expired["installs"].append({"workflow": "z.yml", "job": "j", "command": "pip install x==1", "count": 1,
                                "category": "temporary-deferred", "reason": "r", "expires": "2000-01-01"})  # fmt: skip
    monkeypatch.setattr(ci, "_load_registry", lambda: (expired, []))
    assert any("EXPIRADO" in p for p in ci.problems())


def test_ci_installs_real_tree_is_exact():
    assert ci.problems() == [], ci.problems()
