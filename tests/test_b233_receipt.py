"""B250/B253/B256/B257/B261/B262: el recibo B233 es un DIAGNÓSTICO HISTÓRICO (schema v3) validado por DERIVACIÓN +
PROCEDENCIA + lectura GOBERNADA fd-bound (tools/validate_b233_receipt.py). B261: no existe certificación viva
(capture --certify sale 2, nunca escribe el canónico). B262: el validador es TOTAL (nunca eleva), la versión de Python
es X.Y.Z exacta con major.minor derivado, y la procedencia git (imported_into_repository_at) exige commit real
descendiente de capture_head que AÑADIÓ el recibo."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess

import tools.capture_b233_receipt as cap
import tools.validate_b233_receipt as v

_RECEIPT = os.path.join(v.ROOT, "reports", "governance", "b233_receipt.json")


def _base() -> dict:
    return json.load(open(_RECEIPT))


def test_governed_receipt_v3_is_valid():
    assert os.path.isfile(_RECEIPT)
    assert v.validate_receipt_file(_RECEIPT) == []
    assert v.validate_receipt(_base()) == []
    # forma honesta del comando (B262): argv_display + environment_overrides (no 'argv'/'environment')
    assert set(_base()["capture_command"]) == {"argv_display", "environment_overrides"}


def test_b262_validator_never_raises_on_jsonlike():
    # B262: para CUALQUIER entrada JSON-like devuelve una lista, jamas eleva.
    cases: list[object] = [{}, [], "s", 7, 0, None, True, 3.14, {1: 2}, {"a": object()}, [1, 2, 3]]
    cases.append({1: "x", "schema_version": 3})  # claves mixtas -> antes TypeError en sorted()
    cases.append({k: None for k in v._TOP_KEYS})
    cases.append({**_base(), "extra": 1})
    for i in range(100):  # 100 casos deterministas
        cases.append({"schema_version": i, "capture_head": "z" * (i % 45)})
    for c in cases:
        out = v.validate_receipt(c)
        assert isinstance(out, list), f"validate_receipt debe devolver lista para {type(c)}"


def test_b262_python_version_is_exact_xyz():
    for bad in ("3.14.evil", "3.14.", "3.14", "3.14.2\n", "3.14.2.1", "99.0.0"):
        d = _base()
        d["capture_platform"]["python"] = bad
        assert any("capture_platform" in p for p in v.validate_receipt(d)), f"python={bad!r} debe fallar (B262)"


def test_b262_imported_commit_provenance():
    # commit real que NO contiene/añadió el recibo (base de main) -> falla
    main_base = subprocess.run(
        ["git", "-C", v.ROOT, "rev-parse", "origin/main"], capture_output=True, text=True
    ).stdout.strip()
    for bad in (main_base, "0" * 40, "6d67fd1", "notahex"):
        d = _base()
        d["imported_into_repository_at"] = bad
        assert v.validate_receipt(d) != [], f"imported={bad!r} debe fallar (B262)"
    # un blob como imported tampoco pasa
    blob = subprocess.run(
        ["git", "-C", v.ROOT, "rev-parse", "HEAD:tools/python_env.py"], capture_output=True, text=True
    ).stdout.strip()
    d = _base()
    d["imported_into_repository_at"] = blob
    assert v.validate_receipt(d) != [], "un blob como imported debe fallar"


def test_b262_capture_command_exact_and_honest():
    for mut in (
        {
            "capture_command": {"argv": v._EXPECTED_ARGV_DISPLAY, "environment": v._EXPECTED_ENV_OVERRIDES}
        },  # claves viejas
        {
            "capture_command": {
                "argv_display": v._EXPECTED_ARGV_DISPLAY + ["; rm -rf /"],
                "environment_overrides": v._EXPECTED_ENV_OVERRIDES,
            }
        },  # fmt: skip
        {"capture_command": {"argv_display": v._EXPECTED_ARGV_DISPLAY, "environment_overrides": {"X": "1"}}},
        {"capture_command": "python -m tools.python_env build --profile dev"},
    ):
        d = _base()
        d.update(mut)
        assert v.validate_receipt(d) != [], f"capture_command {mut} debe fallar (B262)"


def test_b262_pep503_collision_rejected():
    # dos nombres que canonicalizan al mismo (visa_predictai vs visa-predictai) en el freeze -> colision
    d = _base()
    d["raw_freeze"] = d["raw_freeze"] + "\nExtra_Pkg==1.0\nextra-pkg==2.0\n"
    d["capture_freeze_sha256"] = hashlib.sha256(d["raw_freeze"].encode()).hexdigest()
    assert any("colisión canónica" in p or "colision" in p.lower() for p in v.validate_receipt(d))


def test_b262_size_limits():
    d = _base()
    d["raw_freeze"] = "a==1\n" * 50000  # excede _MAX_FREEZE_BYTES
    d["capture_freeze_sha256"] = hashlib.sha256(d["raw_freeze"].encode()).hexdigest()
    assert any("tope de tamaño" in p for p in v.validate_receipt(d))


def test_governed_files_procedence_and_platform():
    d = _base()
    first = next(iter(d["governed_files"]))
    d["governed_files"][first] = "sha256:" + "0" * 64
    assert any("procedencia" in p or first in p for p in v.validate_receipt(d))
    for mut in (
        {"system": "EvilOS", "machine": "arm64", "python": "3.14.2"},
        {"system": "Darwin", "machine": "quantum", "python": "3.14.2"},
    ):
        d = _base()
        d["capture_platform"] = mut
        assert any("capture_platform" in p for p in v.validate_receipt(d))


def test_no_toolchain_field_toolchain_is_derived():
    assert "toolchain" not in _base()
    tc, err = v._derive_toolchain()
    assert err is None and tc["setuptools"] and tc["wheel"]
    d = _base()
    d["raw_freeze"] = d["raw_freeze"].replace("setuptools==83.0.0", "setuptools==evil")
    d["capture_freeze_sha256"] = hashlib.sha256(d["raw_freeze"].encode()).hexdigest()
    assert any("observed - expected" in p or "setuptools" in p for p in v.validate_receipt(d))


def test_schema_forgeries():
    for mut in (
        {"schema_version": 2},
        {"schema_version": True},
        {"capture_kind": "live_governed_build_certification"},
        {"return_code": 0},
        {"return_code": True},
        {"extras_exact": []},
        {"extras_exact": ["foo"]},
        {"observed_inventory_size": 999},
    ):
        d = _base()
        d.update(mut)
        assert v.validate_receipt(d) != [], f"forgery {mut} debe fallar"
    d = _base()
    d["backdoor"] = 1
    assert v.validate_receipt(d) != []


def test_governed_reads_are_fd_bound_no_open_by_path():
    import inspect

    src = inspect.getsource(v)
    body = src.split("def _governed_bytes")[0] + src.split("def _validate_receipt")[1]
    assert "open(os.path.join(ROOT" not in body, "no leer un fichero gobernado con open() por ruta (B257)"


# --- B261/B267: capture es verificador/exportador stdout-only; --certify NO disponible hasta R9 ---


def test_b261_certify_refuses_and_never_writes_canonical():
    before = open(_RECEIPT, "rb").read()
    rc = cap.main(["capture_b233_receipt", "--certify"])
    assert rc == 2, "--certify debe salir 2 (pendiente R9/B233)"
    assert open(_RECEIPT, "rb").read() == before, "--certify JAMAS debe escribir el recibo canonico (B261)"


def test_b261_default_verifies():
    assert cap.main(["capture_b233_receipt"]) == 0
    assert cap.main(["capture_b233_receipt", "--verify"]) == 0


def test_b267_export_is_stdout_only_no_out_argument():
    # B267: `--out` ya NO existe (argparse lo rechaza) — no hay export a fichero.
    with __import__("pytest").raises(SystemExit):
        cap.main(["capture_b233_receipt", "--export", "--out", "/tmp/x"])
    import inspect

    src = inspect.getsource(cap)
    assert "O_CREAT" not in src and "O_EXCL" not in src and 'open(dest' not in src, "capture no debe escribir ficheros (B267)"  # fmt: skip
    assert "atómic" not in cap._export.__doc__.lower() or "no promete" in (cap.__doc__ or "").lower()


def test_b267_export_emits_only_validated_bytes():
    # los bytes de --export son EXACTAMENTE los del recibo canónico validado, de UNA sola lectura gobernada.
    data, probs = v.read_and_validate_canonical()
    assert probs == [] and data is not None
    assert json.loads(data.decode())["schema_version"] == 3
    assert data == open(_RECEIPT, "rb").read()


def test_b267_export_refuses_symlink_mode_hardlink(tmp_path, monkeypatch):
    # B267: la lectura gobernada del recibo se niega ante symlink / modo escribible g-o / hardlink → sin bytes.
    gov = tmp_path / "reports" / "governance"
    gov.mkdir(parents=True)
    real = json.loads(open(_RECEIPT).read())
    # symlink canónico -> forjado
    forged = tmp_path / "forged.json"
    forged.write_text('{"forged": true}')
    link = gov / "b233_receipt.json"
    os.symlink(str(forged), str(link))
    monkeypatch.setattr(v, "ROOT", str(tmp_path))
    data, probs = v.read_and_validate_canonical()
    assert data is None and probs, "symlink canónico debe rechazarse (B267)"
    # regular pero escribible por grupo/otros
    os.unlink(str(link))
    (gov / "b233_receipt.json").write_text(json.dumps(real))
    os.chmod(str(gov / "b233_receipt.json"), 0o666)
    data2, probs2 = v.read_and_validate_canonical()
    assert data2 is None and probs2, "recibo 0666 debe rechazarse (B267)"


def test_b267_export_refuses_invalid_json_or_schema(tmp_path, monkeypatch):
    gov = tmp_path / "reports" / "governance"
    gov.mkdir(parents=True)
    (gov / "b233_receipt.json").write_text("{not valid json")
    monkeypatch.setattr(v, "ROOT", str(tmp_path))
    data, probs = v.read_and_validate_canonical()
    assert data is None and probs, "JSON inválido debe rechazarse (B267)"


def test_b261_no_schema4_written_by_capture():
    import inspect

    src = inspect.getsource(cap)
    assert "schema_version" not in src, "capture no debe emitir ningún schema (B261/B267)"
