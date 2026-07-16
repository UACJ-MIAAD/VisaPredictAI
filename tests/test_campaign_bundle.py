"""Bundle inmutable content-addressed + puntero CURRENT por CAS (P0R.5 · B148/B145 · Incremento 1R). Regresiones
adversariales B155-B164: contrato CERRADO, identidad gobernada, CAS con compensación bidireccional, idempotencia.
El bundle es la AUTORIDAD del commit; los consumidores resuelven vía `open_current_bundle()`/`read_current_csv()`."""

from __future__ import annotations

import hashlib
import os

import pytest

import tools.campaign_bundle as cb

_INPUTS = [f"aq_pool_{k}_{t}_{b}.csv" for k in ("nongbm", "gbm") for t in ("FAD", "DFF") for b in ("family", "employment")]  # fmt: skip
_CAMP = [f"campaign_pool_{t}_{b}.csv" for t in ("FAD", "DFF") for b in ("family", "employment")]
_EVAL = ["model_comparison_FAD21.csv", "model_comparison_EB_FAD21.csv", "model_comparison_DFF21.csv", "model_comparison_EB_DFF21.csv"]  # fmt: skip
_H = hashlib.sha256(b"x").hexdigest()


def _camp(tmp_path):
    camp = tmp_path / "reports" / "campaign"
    camp.mkdir(parents=True)
    return camp, os.open(str(camp), os.O_RDONLY | os.O_DIRECTORY)


def _prov():
    return {
        "git_head": None,
        "code_sha_merge_campaign_pools": _H,
        "code_sha_campaign_bundle": _H,
        "code_sha_atomic_fs": _H,
        "code_sha_governed_read": _H,
        "code_sha_execution_contract": None,
        "journal_heads": {},
    }


def _outputs(suffix=b""):
    outs = []
    for n in _CAMP:
        outs.append({"label": "campaign", "name": n, "bytes": b"a,b\n1,2\n" + n.encode() + suffix, "rows": 1, "cols": 2})  # fmt: skip
    for n in _EVAL:
        outs.append({"label": "eval", "name": n, "bytes": b"x,y\n3,4\n" + n.encode() + suffix, "rows": 1, "cols": 2})
    return outs


def _inputs(crlf=False):
    tail = b"\r\n" if crlf else b"\n"
    return [{"name": n, "bytes": b"col" + tail + n.encode() + tail} for n in _INPUTS]


def _manifest_ok():
    ins = [{"name": n, "size": 3, "sha256": _H} for n in _INPUTS]
    outs = [{"label": "campaign", "name": n, "rows": 1, "cols": 2, "sha256": _H} for n in _CAMP]
    outs += [{"label": "eval", "name": n, "rows": 1, "cols": 2, "sha256": _H} for n in _EVAL]
    return cb._manifest_for("campA", "tx.aaaa", ins, outs, _prov())


def _residue(camp):
    return sorted(p.name for p in camp.iterdir() if p.name.startswith((".merge-staging", ".merge-CURRENT.tmp")))


# --------------------------------------------- feliz + content-addressing ---------------------------------------------


def test_build_commit_resolve(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        bid = cb.build_and_commit(cfd, "tx.deadbeef", "campA", _outputs(), _inputs(), _prov())
        assert len(bid) == 64
        rid, manifest = cb.open_current_bundle(cfd)
        assert rid == bid and len(manifest["outputs"]) == 8 and len(manifest["inputs"]) == 8
        assert cb.read_current_csv(cfd, "campaign", "campaign_pool_FAD_family.csv").startswith(b"a,b")
        assert _residue(camp) == []
    finally:
        os.close(cfd)


def test_bundle_id_content_addressed(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        b1 = cb.build_and_commit(cfd, "tx1.aaaa", "campA", _outputs(), _inputs(), _prov())
        b2 = cb.build_and_commit(cfd, "tx2.bbbb", "campA", _outputs(b"z\n"), _inputs(), _prov())
        assert b1 != b2
        cur = cb._read_current(cfd)[0]
        assert cur["bundle_id"] == b2 and cur["previous_bundle_id"] == b1
    finally:
        os.close(cfd)


def test_b164_crlf_input_size_is_real_bytes(tmp_path):
    # B164: el tamaño del input viene de los BYTES REALES (CRLF preservado), no reconstruido con pandas.
    camp, cfd = _camp(tmp_path)
    try:
        cb.build_and_commit(cfd, "tx.crlf", "campA", _outputs(), _inputs(crlf=True), _prov())
        _, man = cb.open_current_bundle(cfd)
        entry = next(e for e in man["inputs"] if e["name"] == "aq_pool_nongbm_FAD_family.csv")
        assert entry["size"] == len(b"col\r\naq_pool_nongbm_FAD_family.csv\r\n")
    finally:
        os.close(cfd)


# --------------------------------------------- B155: validación de nombres ---------------------------------------------


@pytest.mark.parametrize("bad", ["/etc/passwd", "../escape.csv", "a/b.csv", "with\x00nul", ""])
def test_b155_unsafe_output_name_rejected(tmp_path, bad):
    camp, cfd = _camp(tmp_path)
    try:
        outs = _outputs()
        outs[0] = {"label": "campaign", "name": bad, "bytes": b"x", "rows": 1, "cols": 1}
        with pytest.raises(cb.BundleValidationError):
            cb.build_and_commit(cfd, "tx.n", "c", outs, _inputs(), _prov())
        assert _residue(camp) == []  # nada de staging tras el rechazo
    finally:
        os.close(cfd)


@pytest.mark.parametrize("bad", ["/abs.csv", "../x.csv", "d/x.csv", ""])
def test_b155_unsafe_input_name_rejected(tmp_path, bad):
    camp, cfd = _camp(tmp_path)
    try:
        ins = _inputs()
        ins[0] = {"name": bad, "bytes": b"x"}
        with pytest.raises(cb.BundleValidationError):
            cb.build_and_commit(cfd, "tx.n", "c", _outputs(), ins, _prov())
    finally:
        os.close(cfd)


# --------------------------------------------- B159: contrato cerrado ---------------------------------------------


def test_b159_manifest_ok_validates():
    assert cb._validate_manifest(_manifest_ok())["campaign_id"] == "campA"


def test_b159_zero_outputs_rejected():
    m = _manifest_ok()
    m["outputs"] = []
    with pytest.raises(cb.BundleValidationError):
        cb._validate_manifest(m)


def test_b159_missing_output_rejected():
    m = _manifest_ok()
    m["outputs"] = m["outputs"][:-1]
    with pytest.raises(cb.BundleValidationError):
        cb._validate_manifest(m)


def test_b159_extra_output_rejected():
    m = _manifest_ok()
    m["outputs"].append({"label": "campaign", "name": "campaign_pool_FAD_family.csv", "rows": 1, "cols": 2, "sha256": _H})  # fmt: skip
    with pytest.raises(cb.BundleValidationError):
        cb._validate_manifest(m)


def test_b159_bool_schema_version_rejected():
    m = _manifest_ok()
    m["schema_version"] = True  # bool NO es int válido
    with pytest.raises(cb.BundleValidationError):
        cb._validate_manifest(m)


def test_b159_numeric_campaign_id_rejected():
    m = _manifest_ok()
    m["campaign_id"] = 7
    with pytest.raises(cb.BundleValidationError):
        cb._validate_manifest(m)


def test_b159_extra_manifest_key_rejected():
    m = _manifest_ok()
    m["backdoor"] = 1
    with pytest.raises(cb.BundleValidationError):
        cb._validate_manifest(m)


def test_b159_missing_manifest_key_rejected():
    m = _manifest_ok()
    del m["provenance"]
    with pytest.raises(cb.BundleValidationError):
        cb._validate_manifest(m)


@pytest.mark.parametrize("field,badval", [("sha256", "zz"), ("rows", 0), ("cols", -1), ("rows", True)])
def test_b159_bad_output_scalar_rejected(field, badval):
    m = _manifest_ok()
    m["outputs"][0][field] = badval
    with pytest.raises(cb.BundleValidationError):
        cb._validate_manifest(m)


def test_b159_bad_input_size_rejected():
    m = _manifest_ok()
    m["inputs"][0]["size"] = 0
    with pytest.raises(cb.BundleValidationError):
        cb._validate_manifest(m)


def test_b159_incomplete_provenance_rejected():
    m = _manifest_ok()
    del m["provenance"]["code_sha_campaign_bundle"]
    with pytest.raises(cb.BundleValidationError):
        cb._validate_manifest(m)


def test_b159_dup_json_keys_rejected():
    with pytest.raises(cb.BundleValidationError):
        cb._strict_loads(b'{"a":1,"a":2}')


def test_b159_empty_manifest_rejected(tmp_path):
    # un bundle vacío/degenerado no valida como autoridad ni resolviendo por CURRENT.
    camp, cfd = _camp(tmp_path)
    try:
        cb.build_and_commit(cfd, "tx.e", "campA", _outputs(), _inputs(), _prov())
        bid = cb._read_current(cfd)[0]["bundle_id"]
        (camp / ".merge-bundles" / bid / "manifest.json").write_bytes(b"{}")
        with pytest.raises(cb.BundleValidationError):
            cb.open_current_bundle(cfd)
    finally:
        os.close(cfd)


# --------------------------------------------- B160: identidad + inventario ---------------------------------------------


def test_b160_group_writable_sealed_output_rejected(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        bid = cb.build_and_commit(cfd, "tx.g", "campA", _outputs(), _inputs(), _prov())
        os.chmod(camp / ".merge-bundles" / bid / "outputs" / "campaign" / "campaign_pool_FAD_family.csv", 0o666)
        with pytest.raises(cb.BundleValidationError):
            cb.open_current_bundle(cfd)
    finally:
        os.close(cfd)


def test_b160_hardlinked_sealed_output_rejected(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        bid = cb.build_and_commit(cfd, "tx.h", "campA", _outputs(), _inputs(), _prov())
        d = camp / ".merge-bundles" / bid / "outputs" / "campaign"
        os.link(d / "campaign_pool_FAD_family.csv", d / "campaign_pool_FAD_family.csv.hard")
        with pytest.raises(cb.BundleValidationError):
            cb.open_current_bundle(cfd)  # nlink==2 en el sellado + inventario extra
    finally:
        os.close(cfd)


def test_b160_extra_file_in_bundle_rejected(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        bid = cb.build_and_commit(cfd, "tx.x", "campA", _outputs(), _inputs(), _prov())
        (camp / ".merge-bundles" / bid / "EXTRA").write_bytes(b"z")
        with pytest.raises(cb.BundleValidationError):
            cb.open_current_bundle(cfd)
    finally:
        os.close(cfd)


def test_b160_extra_file_in_label_dir_rejected(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        bid = cb.build_and_commit(cfd, "tx.x2", "campA", _outputs(), _inputs(), _prov())
        (camp / ".merge-bundles" / bid / "outputs" / "eval" / "sneak.csv").write_bytes(b"z")
        with pytest.raises(cb.BundleValidationError):
            cb.open_current_bundle(cfd)
    finally:
        os.close(cfd)


def test_b160_tampered_sealed_output_detected(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        bid = cb.build_and_commit(cfd, "tx.t", "campA", _outputs(), _inputs(), _prov())
        p = camp / ".merge-bundles" / bid / "outputs" / "campaign" / "campaign_pool_FAD_family.csv"
        os.chmod(p, 0o600)
        with open(p, "ab") as fh:
            fh.write(b"TAMPER\n")
        with pytest.raises(cb.BundleValidationError):
            cb.read_current_csv(cfd, "campaign", "campaign_pool_FAD_family.csv")
    finally:
        os.close(cfd)


# --------------------------------------------- B161/B162: colisión e idempotencia ---------------------------------------------


def test_b161_collision_with_corrupt_output_does_not_change_current(tmp_path):
    # un bundle preexistente con manifest correcto pero OUTPUT corrupto: al re-commitear el mismo id, la colisión
    # valida el bundle COMPLETO, falla y NO cambia CURRENT (autoridad no envenenada).
    camp, cfd = _camp(tmp_path)
    try:
        first = cb.build_and_commit(cfd, "tx.a", "campA", _outputs(), _inputs(), _prov())
        before = cb._read_current(cfd)[1]
        target = cb.build_and_commit(cfd, "tx.b", "campA", _outputs(b"v2\n"), _inputs(), _prov())  # CURRENT=target
        before2 = cb._read_current(cfd)[1]
        assert cb._read_current(cfd)[0]["bundle_id"] == target and first != target
        # corromper el output sellado de `target` y re-commitear EXACTAMENTE el mismo (colisión de id)
        p = camp / ".merge-bundles" / target / "outputs" / "campaign" / "campaign_pool_FAD_family.csv"
        os.chmod(p, 0o600)
        with open(p, "ab") as fh:
            fh.write(b"corrupt\n")
        with pytest.raises(cb.BundleValidationError):
            cb.build_and_commit(cfd, "tx.b", "campA", _outputs(b"v2\n"), _inputs(), _prov())
        assert cb._read_current(cfd)[1] == before2  # CURRENT intacto pese al intento
        assert before != before2  # (sanity de la secuencia)
    finally:
        os.close(cfd)


def test_b162_repeated_commit_leaves_no_residue(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        for _ in range(3):  # mismo txid+contenido: colisión de bundle; jamás bloquea ni deja staging/tmp
            cb.build_and_commit(cfd, "tx.same", "campA", _outputs(), _inputs(), _prov())
            assert _residue(camp) == []
    finally:
        os.close(cfd)


# --------------------------------------------- B156/B157: CAS correcto ---------------------------------------------


def test_b156_current_race_compensates(tmp_path, monkeypatch):
    # entre _read_current y el exchange, un escritor concurrente cambia CURRENT. El CAS debe COMPENSAR (segundo
    # exchange) y dejar CURRENT byte-idéntico al puntero concurrente, elevando BundleConcurrencyError (no éxito).
    camp, cfd = _camp(tmp_path)
    try:
        cb.build_and_commit(cfd, "tx.a", "campA", _outputs(), _inputs(), _prov())  # CURRENT = A
        concurrent = cb._canon({"schema_version": 1, "campaign_id": "concurrent", "bundle_id": "f" * 64, "previous_bundle_id": None})  # fmt: skip
        prepared = cb.prepare_bundle(cfd, "tx.b", "campA", _outputs(b"b\n"), _inputs(), _prov())
        real_ex = cb.rename_exchange
        state = {"n": 0}

        def racing_exchange(sfd, s, dfd, d):
            state["n"] += 1
            if state["n"] == 1:  # el "escritor concurrente" reemplaza el contenido de CURRENT en sitio
                w = os.open(cb._CURRENT_NAME, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, dir_fd=cfd)
                os.write(w, concurrent)
                os.fsync(w)
                os.close(w)
            return real_ex(sfd, s, dfd, d)

        monkeypatch.setattr(cb, "rename_exchange", racing_exchange)
        with pytest.raises(cb.BundleConcurrencyError):
            cb.commit_current(prepared)
        monkeypatch.undo()
        assert cb._read_current(cfd)[1] == concurrent  # CURRENT restaurado al concurrente, byte-idéntico
        assert _residue(camp) == []  # mi puntero withdrawn, sin .tmp suelto
    finally:
        os.close(cfd)


def test_b157_precommit_write_failure_leaves_current_intact(tmp_path, monkeypatch):
    # un fallo al escribir el nuevo puntero (antes del CAS) NO debe tocar CURRENT (sin truncar ni corromper).
    camp, cfd = _camp(tmp_path)
    try:
        cb.build_and_commit(cfd, "tx.a", "campA", _outputs(), _inputs(), _prov())
        before = cb._read_current(cfd)[1]
        prepared = cb.prepare_bundle(cfd, "tx.b", "campA", _outputs(b"b\n"), _inputs(), _prov())
        real_write_all = cb._write_all

        def failing_write_all(fd, data):
            if b"previous_bundle_id" in data:  # sólo el puntero CURRENT
                raise cb.BundleError("escritura del puntero simulada fallida")
            return real_write_all(fd, data)

        monkeypatch.setattr(cb, "_write_all", failing_write_all)
        with pytest.raises(cb.BundleError):
            cb.commit_current(prepared)
        monkeypatch.undo()
        assert cb._read_current(cfd)[1] == before  # CURRENT sin cambios
        assert _residue(camp) == []  # el tmp fallido se limpió
    finally:
        os.close(cfd)


def test_b157_postcas_failure_is_committed_state(tmp_path, monkeypatch):
    # un fallo DESPUÉS de que el CAS cruzó debe ser CommittedStateError (nunca un rollback silencioso); CURRENT ya
    # avanzó al bundle nuevo.
    camp, cfd = _camp(tmp_path)
    try:
        a = cb.build_and_commit(cfd, "tx.a", "campA", _outputs(), _inputs(), _prov())
        prepared = cb.prepare_bundle(cfd, "tx.b", "campA", _outputs(b"b\n"), _inputs(), _prov())
        monkeypatch.setattr(cb, "_verify_current", lambda *a, **k: (_ for _ in ()).throw(cb.BundleError("post-CAS")))
        with pytest.raises(cb.CommittedStateError):
            cb.commit_current(prepared)
        monkeypatch.undo()
        assert cb._read_current(cfd)[0]["bundle_id"] == prepared.bundle_id != a  # el commit cruzó
    finally:
        os.close(cfd)


# --------------------------------------------- B163: lector por snapshot único ---------------------------------------------


def test_b163_snapshot_reads_consistent_across_current_swap(tmp_path):
    # un snapshot abierto sigue leyendo su bundle aunque CURRENT cambie a otra campaña entre lecturas (no mezcla).
    camp, cfd = _camp(tmp_path)
    try:
        a = cb.build_and_commit(cfd, "tx.a", "campA", _outputs(), _inputs(), _prov())
        with cb.open_current_snapshot(cfd) as snap:
            first = snap.read("campaign", "campaign_pool_FAD_family.csv")
            b = cb.build_and_commit(cfd, "tx.b", "campA", _outputs(b"NEW\n"), _inputs(), _prov())  # CURRENT → b
            assert a != b and snap.bundle_id == a
            second = snap.read("campaign", "campaign_pool_DFF_family.csv")
            assert first.startswith(b"a,b") and second.startswith(b"a,b")  # ambos del bundle A
    finally:
        os.close(cfd)


def test_no_current_raises(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        with pytest.raises(cb.BundleError):
            cb.open_current_bundle(cfd)
    finally:
        os.close(cfd)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
