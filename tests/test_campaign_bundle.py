"""Bundle inmutable content-addressed + puntero CURRENT por CAS (P0R.5 · B148/B145 · Incrementos 1R/1R2). Regresiones
adversariales B155-B174: contrato cerrado, identidad gobernada, verificación CSV, CAS fd-vivo con compensación por
contenido, limpieza fail-closed, snapshot sin fugas. El bundle es la AUTORIDAD del commit."""

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
    # module-adaptivo: emite EXACTAMENTE las claves que el módulo cargado exige, para que el proof red-first aísle
    # cada vulnerabilidad (no un desajuste de esquema de procedencia) en cualquier versión del módulo.
    base = {
        "git_head": None,
        "code_sha_merge_campaign_pools": _H,
        "code_sha_campaign_bundle": _H,
        "code_sha_atomic_fs": _H,
        "code_sha_governed_read": _H,
        "code_sha_execution_contract": None,
        "journal_heads": {},
        "python": "3.14.2",
        "platform": "darwin",
        "profile": None,
        "variant": None,
    }
    return {k: base[k] for k in cb._REQUIRED_PROVENANCE}


def _outputs(suffix=b""):
    outs = []
    for n in _CAMP:
        outs.append({"label": "campaign", "name": n, "bytes": b"a,b\n1," + n.encode() + suffix + b"\n", "rows": 1, "cols": 2})  # fmt: skip
    for n in _EVAL:
        outs.append({"label": "eval", "name": n, "bytes": b"x,y\n3," + n.encode() + suffix + b"\n", "rows": 1, "cols": 2})  # fmt: skip
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


def _commit(cfd, txid="tx.aaaa", suffix=b""):
    return cb.build_and_commit(cfd, txid, "campA", _outputs(suffix), _inputs(), _prov())


# --------------------------------------------- feliz + content-addressing ---------------------------------------------


def test_build_commit_resolve(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        bid = _commit(cfd, "tx.deadbeef")
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
        b1 = _commit(cfd, "tx1.aaaa")
        b2 = _commit(cfd, "tx2.bbbb", suffix=b"z")
        assert b1 != b2
        cur = cb._read_current(cfd)[0]
        assert cur["bundle_id"] == b2 and cur["previous_bundle_id"] == b1
    finally:
        os.close(cfd)


def test_b164_crlf_input_size_is_real_bytes(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        cb.build_and_commit(cfd, "tx.crlf", "campA", _outputs(), _inputs(crlf=True), _prov())
        _, man = cb.open_current_bundle(cfd)
        entry = next(e for e in man["inputs"] if e["name"] == "aq_pool_nongbm_FAD_family.csv")
        assert entry["size"] == len(b"col\r\naq_pool_nongbm_FAD_family.csv\r\n")
    finally:
        os.close(cfd)


# --------------------------------------------- B155: nombres ---------------------------------------------


@pytest.mark.parametrize("bad", ["/etc/passwd", "../escape.csv", "a/b.csv", "with\x00nul", ""])
def test_b155_unsafe_output_name_rejected(tmp_path, bad):
    camp, cfd = _camp(tmp_path)
    try:
        outs = _outputs()
        outs[0] = {"label": "campaign", "name": bad, "bytes": b"a,b\n1,2\n", "rows": 1, "cols": 2}
        with pytest.raises(cb.BundleValidationError):
            cb.build_and_commit(cfd, "tx.n", "c", outs, _inputs(), _prov())
        assert _residue(camp) == []
    finally:
        os.close(cfd)


# --------------------------------------------- B159/B168: esquemas cerrados ---------------------------------------------


def test_b159_manifest_ok_validates():
    assert cb._validate_manifest(_manifest_ok())["campaign_id"] == "campA"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.update(outputs=[]),
        lambda m: m.update(outputs=m["outputs"][:-1]),
        lambda m: m["outputs"].append(
            {"label": "campaign", "name": "campaign_pool_FAD_family.csv", "rows": 1, "cols": 2, "sha256": _H}
        ),  # fmt: skip
        lambda m: m.update(schema_version=True),
        lambda m: m.update(campaign_id=7),
        lambda m: m.update(backdoor=1),
        lambda m: m.pop("provenance"),
        lambda m: m["outputs"][0].update(sha256="zz"),
        lambda m: m["outputs"][0].update(rows=True),
        lambda m: m["outputs"][0].update(cols=0),
        lambda m: m["inputs"][0].update(size=0),
        lambda m: m["provenance"].pop("code_sha_campaign_bundle"),
    ],
)
def test_b159_closed_manifest_rejects(mutate):
    m = _manifest_ok()
    mutate(m)
    with pytest.raises(cb.BundleValidationError):
        cb._validate_manifest(m)


def test_b159_dup_json_keys_rejected():
    with pytest.raises(cb.BundleValidationError):
        cb._strict_loads(b'{"a":1,"a":2}')


def test_b168_malformed_json_is_governed_error():
    with pytest.raises(cb.BundleValidationError):
        cb._strict_loads(b"{not json")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(schema_version=True),
        lambda p: p.pop("previous_bundle_id"),
        lambda p: p.update(backdoor=1),
        lambda p: p.update(bundle_id="short"),
        lambda p: p.update(campaign_id="   "),
    ],
)
def test_b168_closed_pointer_schema_rejects(mutate):
    p = {"schema_version": 1, "campaign_id": "c", "bundle_id": _H, "previous_bundle_id": None}
    mutate(p)
    with pytest.raises(cb.BundleValidationError):
        cb._validate_pointer(p)


# --------------------------------------------- B169: CSV real ---------------------------------------------


def test_b169_fake_row_count_rejected(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        outs = _outputs()
        outs[0]["rows"] = 999  # metadato falso para un CSV de una fila
        with pytest.raises(cb.BundleValidationError):
            cb.build_and_commit(cfd, "tx.r", "campA", outs, _inputs(), _prov())
    finally:
        os.close(cfd)


def test_b169_string_cols_rejected(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        outs = _outputs()
        outs[0]["cols"] = "2"  # str, no int
        with pytest.raises(cb.BundleValidationError):
            cb.build_and_commit(cfd, "tx.c", "campA", outs, _inputs(), _prov())
    finally:
        os.close(cfd)


def test_b169_duplicate_columns_rejected():
    with pytest.raises(cb.BundleValidationError):
        cb._verify_csv(b"a,a\n1,2\n", 1, 2)


# --------------------------------------------- B171: procedencia ---------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda pr: pr.update(git_head="x"),
        lambda pr: pr.update(code_sha_atomic_fs=None),
        lambda pr: pr.update(journal_heads={"bogus": _H}),
        lambda pr: pr.update(journal_heads={"campaign": "nothex"}),
        lambda pr: pr.update(python=""),
        lambda pr: pr.pop("platform"),
    ],
)
def test_b171_provenance_schema_rejects(mutate):
    m = _manifest_ok()
    mutate(m["provenance"])
    with pytest.raises(cb.BundleValidationError):
        cb._validate_manifest(m)


# --------------------------------------------- B160/B172: identidad + modo ---------------------------------------------


def test_b160_group_writable_sealed_output_rejected(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        bid = _commit(cfd, "tx.g")
        os.chmod(camp / ".merge-bundles" / bid / "outputs" / "campaign" / "campaign_pool_FAD_family.csv", 0o666)
        with pytest.raises(cb.BundleValidationError):
            cb.open_current_bundle(cfd)
    finally:
        os.close(cfd)


def test_b160_hardlinked_sealed_output_rejected(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        bid = _commit(cfd, "tx.h")
        d = camp / ".merge-bundles" / bid / "outputs" / "campaign"
        os.link(d / "campaign_pool_FAD_family.csv", d / "campaign_pool_FAD_family.csv.hard")
        with pytest.raises(cb.BundleValidationError):
            cb.open_current_bundle(cfd)
    finally:
        os.close(cfd)


def test_b160_extra_file_in_label_dir_rejected(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        bid = _commit(cfd, "tx.x2")
        (camp / ".merge-bundles" / bid / "outputs" / "eval" / "sneak.csv").write_bytes(b"z")
        with pytest.raises(cb.BundleValidationError):
            cb.open_current_bundle(cfd)
    finally:
        os.close(cfd)


def test_b172_non_0700_sealed_dir_rejected(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        bid = _commit(cfd, "tx.m")
        os.chmod(camp / ".merge-bundles" / bid / "outputs" / "campaign", 0o755)
        with pytest.raises(cb.BundleValidationError):
            cb.open_current_bundle(cfd)
    finally:
        os.close(cfd)


# --------------------------------------------- B161/B162/B173: colisión, residuo, autoridad previa ---------------------------------------------


def test_b161_collision_with_corrupt_output_blocks(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        _commit(cfd, "tx.a")
        target = _commit(cfd, "tx.b", suffix=b"v2")
        before = cb._read_current(cfd)[1]
        p = camp / ".merge-bundles" / target / "outputs" / "campaign" / "campaign_pool_FAD_family.csv"
        os.chmod(p, 0o600)
        with open(p, "ab") as fh:
            fh.write(b"corrupt\n")
        with pytest.raises(cb.BundleValidationError):
            cb.build_and_commit(cfd, "tx.b", "campA", _outputs(b"v2"), _inputs(), _prov())
        assert cb._read_current(cfd)[1] == before  # CURRENT intacto
    finally:
        os.close(cfd)


def test_b162_repeated_commit_leaves_no_residue(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        for _ in range(3):
            _commit(cfd, "tx.same")
            assert _residue(camp) == []
    finally:
        os.close(cfd)


def test_b173_invalid_prior_authority_blocks_new_commit(tmp_path):
    # si el bundle al que apunta el CURRENT previo está corrupto, el nuevo commit BLOQUEA (no lo repara en silencio).
    camp, cfd = _camp(tmp_path)
    try:
        a = _commit(cfd, "tx.a")
        before = cb._read_current(cfd)[1]
        p = camp / ".merge-bundles" / a / "outputs" / "campaign" / "campaign_pool_FAD_family.csv"
        os.chmod(p, 0o600)
        with open(p, "ab") as fh:
            fh.write(b"corrupt\n")
        with pytest.raises(cb.BundleValidationError):
            _commit(cfd, "tx.b", suffix=b"new")
        assert cb._read_current(cfd)[1] == before
    finally:
        os.close(cfd)


# --------------------------------------------- B165: _Prepared no confiable ---------------------------------------------


def test_b165_fabricated_prepared_rejected(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        _commit(cfd, "tx.a")  # existe un bundles dir
        # _PreparedBundle apunta a un bundle_id inexistente → revalidate/commit deben rechazar
        with pytest.raises(cb.BundleValidationError):
            cb._PreparedBundle(cfd, "f" * 64, "campA", _manifest_ok())
    finally:
        os.close(cfd)


def test_b165_bundle_altered_after_prepare_rejected(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        with cb.prepare_bundle(cfd, "tx.a", "campA", _outputs(), _inputs(), _prov()) as prepared:
            p = camp / ".merge-bundles" / prepared.bundle_id / "outputs" / "campaign" / "campaign_pool_FAD_family.csv"
            os.chmod(p, 0o600)
            with open(p, "ab") as fh:
                fh.write(b"tamper\n")
            with pytest.raises(cb.BundleValidationError):  # commit re-valida a través del fd vivo → detecta
                cb.commit_current(prepared)
    finally:
        os.close(cfd)


# --------------------------------------------- B166/B167: CAS por contenido + compensación ---------------------------------------------


def test_b166_inplace_pointer_substitution_never_publishes_attacker(tmp_path, monkeypatch):
    # sustituir el contenido del puntero temporal en sitio durante el exchange NO debe dejar CURRENT apuntando al
    # bundle del atacante: se detecta por contenido y CURRENT permanece en el bundle válido previo.
    camp, cfd = _camp(tmp_path)
    try:
        a = _commit(cfd, "tx.a")
        with cb.prepare_bundle(cfd, "tx.b", "campA", _outputs(b"b"), _inputs(), _prov()) as prepared:
            real_ex = cb.rename_exchange
            state = {"n": 0}

            def substituting_exchange(sfd, s, dfd, d):
                state["n"] += 1
                if state["n"] == 1:  # reescribe el tmp EN SITIO con el puntero del atacante antes del exchange real
                    fd = os.open(s, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, dir_fd=sfd)
                    os.write(fd, b'{"schema_version":1,"campaign_id":"evil","bundle_id":"' + b"e" * 64 + b'","previous_bundle_id":null}')  # fmt: skip
                    os.fsync(fd)
                    os.close(fd)
                return real_ex(sfd, s, dfd, d)

            monkeypatch.setattr(cb, "rename_exchange", substituting_exchange)
            with pytest.raises(cb.BundleError):  # el contenido no liga a mi puntero → carrera; estado preservado
                cb.commit_current(prepared)
            monkeypatch.undo()
        assert cb.open_current_bundle(cfd)[0] == a  # CURRENT sigue en el bundle VÁLIDO previo, jamás en 'e'*64
    finally:
        os.close(cfd)


def test_b156_current_race_restores_concurrent_value(tmp_path, monkeypatch):
    # un cambio concurrente de CURRENT entre lectura y exchange → compensa dejando el valor CONCURRENTE, byte-idéntico.
    camp, cfd = _camp(tmp_path)
    try:
        _commit(cfd, "tx.a")
        concurrent = cb._canon({"schema_version": 1, "campaign_id": "concurrent", "bundle_id": "f" * 64, "previous_bundle_id": None})  # fmt: skip
        with cb.prepare_bundle(cfd, "tx.b", "campA", _outputs(b"b"), _inputs(), _prov()) as prepared:
            real_ex = cb.rename_exchange
            state = {"n": 0}

            def racing_exchange(sfd, s, dfd, d):
                state["n"] += 1
                if state["n"] == 1:
                    fd = os.open(cb._CURRENT_NAME, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, dir_fd=cfd)
                    os.write(fd, concurrent)
                    os.fsync(fd)
                    os.close(fd)
                return real_ex(sfd, s, dfd, d)

            monkeypatch.setattr(cb, "rename_exchange", racing_exchange)
            with pytest.raises(cb.BundleConcurrencyError):
                cb.commit_current(prepared)
            monkeypatch.undo()
        assert cb._read_current(cfd)[1] == concurrent  # valor concurrente restaurado byte-idéntico
        assert _residue(camp) == []
    finally:
        os.close(cfd)


def test_b167_failed_compensation_preserves_and_raises(tmp_path, monkeypatch):
    # si el segundo exchange (compensación) falla, se PRESERVA todo y se eleva BundleRollbackIncompleteError.
    camp, cfd = _camp(tmp_path)
    try:
        _commit(cfd, "tx.a")
        concurrent = cb._canon({"schema_version": 1, "campaign_id": "conc", "bundle_id": "f" * 64, "previous_bundle_id": None})  # fmt: skip
        with cb.prepare_bundle(cfd, "tx.b", "campA", _outputs(b"b"), _inputs(), _prov()) as prepared:
            real_ex = cb.rename_exchange
            state = {"n": 0}

            def failing_second(sfd, s, dfd, d):
                state["n"] += 1
                if state["n"] == 1:
                    fd = os.open(cb._CURRENT_NAME, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, dir_fd=cfd)
                    os.write(fd, concurrent)
                    os.fsync(fd)
                    os.close(fd)
                    return real_ex(sfd, s, dfd, d)
                raise OSError("compensación simulada rota")  # el 2º exchange (compensación) falla

            monkeypatch.setattr(cb, "rename_exchange", failing_second)
            with pytest.raises(cb.BundleRollbackIncompleteError):
                cb.commit_current(prepared)
            monkeypatch.undo()
    finally:
        os.close(cfd)


# --------------------------------------------- B170: limpieza fail-closed ---------------------------------------------


def test_b170_cleanup_failure_is_not_silent(tmp_path, monkeypatch):
    # si la limpieza del staging falla, NO se devuelve éxito silencioso.
    camp, cfd = _camp(tmp_path)
    try:
        monkeypatch.setattr(cb, "_rmtree_governed", lambda *a, **k: (_ for _ in ()).throw(OSError("cleanup roto")))
        with pytest.raises(cb.BundleError):
            _commit(cfd, "tx.a")
    finally:
        os.close(cfd)


# --------------------------------------------- B174: snapshot sin fugas ---------------------------------------------


def test_b174_snapshot_consistent_across_current_swap(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        a = _commit(cfd, "tx.a")
        with cb.open_current_snapshot(cfd) as snap:
            first = snap.read("campaign", "campaign_pool_FAD_family.csv")
            b = _commit(cfd, "tx.b", suffix=b"NEW")
            assert a != b and snap.bundle_id == a
            second = snap.read("campaign", "campaign_pool_DFF_family.csv")
            assert first.startswith(b"a,b") and second.startswith(b"a,b")
    finally:
        os.close(cfd)


def test_b174_snapshot_partial_open_failure_no_fd_leak(tmp_path, monkeypatch):
    # si una apertura parcial del snapshot falla, no deben quedar descriptores abiertos.
    camp, cfd = _camp(tmp_path)
    try:
        _commit(cfd, "tx.a")
        before = len(os.listdir("/dev/fd")) if os.path.isdir("/dev/fd") else len(os.listdir("/proc/self/fd"))
        real_open_dir = cb._open_dir
        state = {"n": 0}

        def flaky_open_dir(parent_fd, name, **k):
            state["n"] += 1
            if state["n"] == 4:  # falla en una apertura intermedia (un label)
                raise OSError("apertura parcial simulada")
            return real_open_dir(parent_fd, name, **k)

        monkeypatch.setattr(cb, "_open_dir", flaky_open_dir)
        with pytest.raises(OSError):
            cb.open_current_bundle(cfd)
        monkeypatch.undo()
        after = len(os.listdir("/dev/fd")) if os.path.isdir("/dev/fd") else len(os.listdir("/proc/self/fd"))
        assert after <= before, f"fuga de fds: {before} -> {after}"
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
