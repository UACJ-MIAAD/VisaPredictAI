"""CLI `validate-current` (P0R.5 · Incremento 2): valida la autoridad CURRENT (puntero+bundle+manifiesto+contrato+
linaje) fail-closed, emite JSON canónico y NO repara. rc 0 sólo si la autoridad es válida."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

import tools.campaign_bundle as cb
import tools.lock_contracts as lc

ROOT = lc.ROOT


def _build_current(tmp_path):
    """Construye una autoridad CURRENT REAL corriendo el merge sobre las 8 mitades reales; devuelve la ruta de campaign."""
    camp = tmp_path / "reports" / "campaign"
    ev = tmp_path / "reports" / "eval"
    camp.mkdir(parents=True)
    ev.mkdir(parents=True)
    src = ROOT / "reports" / "campaign"
    for f in os.listdir(src):
        if f.startswith("aq_pool_"):
            shutil.copy(src / f, camp)
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    r = subprocess.run([sys.executable, "-m", "tools.merge_campaign_pools"], cwd=str(tmp_path), env=env, capture_output=True, text=True)  # fmt: skip
    assert r.returncode == 0, f"merge falló: {r.stderr}"
    return camp


def _run(camp):
    import threading

    holder: dict = {}

    def go():
        holder["rc"] = cb._cli_validate_current(str(camp))

    t = threading.Thread(target=go, daemon=True)  # con timeout: un objeto especial no debe colgar la CLI
    t.start()
    t.join(6)
    assert not t.is_alive(), "validate-current COLGÓ"
    return holder["rc"]


def _pointer(camp):
    return camp / ".merge-CURRENT"


def test_valid_authority(tmp_path, capsys):
    camp = _build_current(tmp_path)
    rc = _run(camp)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["status"] == "valid"
    assert len(out["bundle_id"]) == 64 and out["csv_contract_sha256"] == cb._CSV_CONTRACT_SHA256
    assert out["n_inputs"] >= 1 and out["n_outputs"] >= 1


def test_absent_current(tmp_path, capsys):
    camp = _build_current(tmp_path)
    os.remove(_pointer(camp))
    assert _run(camp) == 3 and json.loads(capsys.readouterr().out)["status"] == "invalid"


def test_corrupt_pointer(tmp_path, capsys):
    camp = _build_current(tmp_path)
    os.chmod(_pointer(camp), 0o600)
    _pointer(camp).write_text("garbage-not-json")
    assert _run(camp) == 3 and json.loads(capsys.readouterr().out)["status"] == "invalid"


def test_missing_bundle(tmp_path, capsys):
    camp = _build_current(tmp_path)
    bundles = camp / ".merge-bundles"
    for d in os.listdir(bundles):  # borra el árbol del bundle apuntado
        shutil.rmtree(bundles / d)
    assert _run(camp) == 3 and json.loads(capsys.readouterr().out)["status"] == "invalid"


def test_altered_manifest(tmp_path, capsys):
    camp = _build_current(tmp_path)
    bundles = camp / ".merge-bundles"
    bid = next(iter(os.listdir(bundles)))
    man = bundles / bid / "manifest.json"
    os.chmod(man, 0o600)
    data = json.loads(man.read_text())
    data["txid"] = str(data.get("txid", "")) + "X"  # cambia el canónico → bundle_id ya no == sha(manifest)
    man.write_text(json.dumps(data))
    assert _run(camp) == 3 and json.loads(capsys.readouterr().out)["status"] == "invalid"


def test_altered_output_csv(tmp_path, capsys):
    camp = _build_current(tmp_path)
    bundles = camp / ".merge-bundles"
    bid = next(iter(os.listdir(bundles)))
    outs = bundles / bid / "outputs"
    # muta el primer CSV de output sellado → inventario/digest ya no cuadra
    for label in os.listdir(outs):
        for name in os.listdir(outs / label):
            p = outs / label / name
            os.chmod(p, 0o600)
            p.write_bytes(p.read_bytes() + b"9,9,9\n")
            assert _run(camp) == 3 and json.loads(capsys.readouterr().out)["status"] == "invalid"
            return
    pytest.skip("sin outputs sellados")


@pytest.mark.parametrize("kind", ["fifo", "symlink"])
def test_special_pointer_no_hang(tmp_path, capsys, kind):
    camp = _build_current(tmp_path)
    os.remove(_pointer(camp))
    if kind == "fifo":
        os.mkfifo(_pointer(camp))
    else:
        (camp / "elsewhere").write_text("x")
        os.symlink(str(camp / "elsewhere"), str(_pointer(camp)))
    assert _run(camp) == 3  # no cuelga (thread+timeout en _run) y rechaza
    assert json.loads(capsys.readouterr().out)["status"] in ("invalid", "error")


def test_non_governed_dir_rejected(tmp_path, capsys):
    # un directorio escribible por grupo/otros no es una campaña gobernada → error, sin validar.
    camp = _build_current(tmp_path)
    os.chmod(camp, 0o777)
    assert _run(camp) == 2 and json.loads(capsys.readouterr().out)["status"] == "error"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_forged_campaign_id_divergence(tmp_path, capsys):
    # B227: mutar SOLO CURRENT.campaign_id (puntero) sin tocar el manifiesto → validate-current DEBE rechazar
    # (la resolucion cruza puntero<->manifiesto, no solo valida cada objeto por separado).
    import json as _j

    camp = _build_current(tmp_path)
    cur = _pointer(camp)
    p = _j.loads(cur.read_bytes())
    p["campaign_id"] = "FORGED-CAMPAIGN"
    os.chmod(cur, 0o600)
    fd = os.open(str(cur), os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
    os.write(fd, cb._canon(p))
    os.close(fd)
    assert _run(camp) == 3 and json.loads(capsys.readouterr().out)["status"] == "invalid"
