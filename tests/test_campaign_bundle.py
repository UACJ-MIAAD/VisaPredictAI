"""Bundle inmutable content-addressed + puntero CURRENT por CAS (P0R.5 · B148/B145 · Incrementos 1R/1R2/1R3).
Regresiones adversariales B155-B188: contrato cerrado, contrato CSV real, procedencia oficial, handle inmutable de
uso único, CAS con estado explícito + cuarentena gobernada (sin check→unlink), compensación bidireccional con
validación de autoridad. El bundle es la AUTORIDAD del commit."""

from __future__ import annotations

import hashlib
import json
import os

import pytest

import tools.campaign_bundle as cb
import tools.governed_fs as gf

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(cb.__file__)))
_COLS = json.load(open(os.path.join(_ROOT, "security", "campaign_bundle_contract.json")))["columns"]
_HDR = ",".join(_COLS)
_INPUTS = [f"aq_pool_{k}_{t}_{b}.csv" for k in ("nongbm", "gbm") for t in ("FAD", "DFF") for b in ("family", "employment")]  # fmt: skip
_CAMP = [f"campaign_pool_{t}_{b}.csv" for t in ("FAD", "DFF") for b in ("family", "employment")]
_EVAL = ["model_comparison_FAD21.csv", "model_comparison_EB_FAD21.csv", "model_comparison_DFF21.csv", "model_comparison_EB_DFF21.csv"]  # fmt: skip
_H = hashlib.sha256(b"x").hexdigest()


def _camp(tmp_path):
    camp = tmp_path / "reports" / "campaign"
    camp.mkdir(parents=True)
    return camp, os.open(str(camp), os.O_RDONLY | os.O_DIRECTORY)


def _prov(**over):
    base = {
        "mode": "test",
        "git_head": None,
        "git_tree": None,
        "git_dirty": None,
        "env_id": None,
        "code_sha_merge_campaign_pools": _H,
        "code_sha_campaign_bundle": _H,
        "code_sha_atomic_fs": _H,
        "code_sha_governed_read": _H,
        "code_sha_execution_contract": None,
        "csv_contract_sha256": getattr(cb, "_CSV_CONTRACT_SHA256", _H),  # module-adaptivo (rojo limpio en SHA viejo)
        "journal_heads": {},
        "python": "3.14.2",
        "platform": "darwin",
        "profile": None,
        "variant": None,
    }
    base.update(over)
    return {k: base[k] for k in cb._REQUIRED_PROVENANCE}


def _row(tag):
    return ",".join([str(tag)] + ["0"] * (len(_COLS) - 1))


def _csv(tag, rows=1):
    return (_HDR + "\n" + "\n".join(_row(f"{tag}{i}") for i in range(rows)) + "\n").encode()


def _outputs(suffix=""):
    outs = []
    for n in _CAMP:
        outs.append({"label": "campaign", "name": n, "bytes": _csv("c" + n + suffix), "rows": 1, "cols": len(_COLS)})
    for n in _EVAL:
        outs.append({"label": "eval", "name": n, "bytes": _csv("e" + n + suffix), "rows": 1, "cols": len(_COLS)})
    return outs


def _inputs(crlf=False):
    tail = b"\r\n" if crlf else b"\n"
    return [{"name": n, "bytes": b"col" + tail + n.encode() + tail} for n in _INPUTS]


def _manifest_ok():
    ins = [{"name": n, "size": 3, "sha256": _H} for n in _INPUTS]
    outs = [{"label": "campaign", "name": n, "rows": 1, "cols": len(_COLS), "sha256": _H} for n in _CAMP]
    outs += [{"label": "eval", "name": n, "rows": 1, "cols": len(_COLS), "sha256": _H} for n in _EVAL]
    return cb._manifest_for("campA", "tx.aaaa", ins, outs, _prov())


def _residue(camp):
    # SÓLO temporales de la RUTA OFICIAL. `.merge-quar.*` es cuarentena MOVE-ONLY: PRESERVADA a propósito (GC futuro).
    return sorted(p.name for p in camp.iterdir() if p.name.startswith((".merge-staging", ".merge-CURRENT.tmp")))


def _quarantines(camp):
    return [p for p in camp.iterdir() if p.name.startswith(".merge-quar")]


def _commit(cfd, txid="tx.aaaa", suffix=""):
    return cb.build_and_commit(cfd, txid, "campA", _outputs(suffix), _inputs(), _prov())


# --------------------------------------------- feliz + content-addressing ---------------------------------------------


def test_build_commit_resolve(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        bid = _commit(cfd, "tx.deadbeef")
        assert len(bid) == 64
        rid, manifest = cb.open_current_bundle(cfd)
        assert rid == bid and len(manifest["outputs"]) == 8 and len(manifest["inputs"]) == 8
        assert cb.read_current_csv(cfd, "campaign", "campaign_pool_FAD_family.csv").startswith(_HDR.encode())
        assert _residue(camp) == []
    finally:
        os.close(cfd)


def test_content_addressed_and_chain(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        b1 = _commit(cfd, "tx1")
        b2 = _commit(cfd, "tx2", suffix="z")
        assert b1 != b2
        cur = cb._read_current(cfd)[0]
        assert cur["bundle_id"] == b2 and cur["previous_bundle_id"] == b1
    finally:
        os.close(cfd)


# --------------------------------------------- B159/B168: esquemas cerrados ---------------------------------------------


def test_b159_manifest_ok_validates():
    assert cb._validate_manifest(_manifest_ok())["campaign_id"] == "campA"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.update(outputs=[]),
        lambda m: m.update(schema_version=True),
        lambda m: m.update(campaign_id=7),
        lambda m: m.update(backdoor=1),
        lambda m: m["outputs"][0].update(rows=True),
        lambda m: m["outputs"][0].update(sha256="zz"),
        lambda m: m["inputs"][0].update(size=0),
        lambda m: m["provenance"].pop("code_sha_campaign_bundle"),
    ],
)
def test_b159_closed_manifest_rejects(mutate):
    m = _manifest_ok()
    mutate(m)
    with pytest.raises(cb.BundleValidationError):
        cb._validate_manifest(m)


def test_b168_dup_keys_and_malformed_json():
    with pytest.raises(cb.BundleValidationError):
        cb._strict_loads(b'{"a":1,"a":2}')
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
def test_b168_closed_pointer_schema(mutate):
    p = {"schema_version": 1, "campaign_id": "c", "bundle_id": _H, "previous_bundle_id": None}
    mutate(p)
    with pytest.raises(cb.BundleValidationError):
        cb._validate_pointer(p)


# --------------------------------------------- B169/B186: contrato CSV real ---------------------------------------------


def test_b169_fake_row_count_rejected(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        outs = _outputs()
        outs[0]["rows"] = 999
        with pytest.raises(cb.BundleValidationError):
            cb.build_and_commit(cfd, "tx.r", "campA", outs, _inputs(), _prov())
    finally:
        os.close(cfd)


def test_b186_wrong_header_rejected():
    with pytest.raises(cb.BundleValidationError):
        cb._verify_csv(b"a,b\n1,2\n", 1, 2)  # header arbitrario a,b


def test_b186_exact_header_but_reordered_rejected():
    reordered = ",".join([_COLS[1], _COLS[0]] + _COLS[2:])
    with pytest.raises(cb.BundleValidationError):
        cb._verify_csv((reordered + "\n" + _row("x") + "\n").encode(), 1, len(_COLS))


def test_b186_bom_rejected():
    with pytest.raises(cb.BundleValidationError):
        cb._verify_csv(b"\xef\xbb\xbf" + _csv("x"), 1, len(_COLS))


def test_b186_non_utf8_rejected():
    with pytest.raises(cb.BundleValidationError):
        cb._verify_csv(_HDR.encode() + b"\n" + b"\xff\xfe" + b",".join([b"0"] * (len(_COLS) - 1)) + b"\n", 1, len(_COLS))  # fmt: skip


def test_b186_valid_contract_header_passes():
    cb._verify_csv(_csv("x", rows=3), 3, len(_COLS))


# --------------------------------------------- B171/B187: procedencia ---------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda pr: pr.update(git_head="x"),
        lambda pr: pr.update(code_sha_atomic_fs=None),
        lambda pr: pr.update(journal_heads={"bogus": _H}),
        lambda pr: pr.update(python=""),
        lambda pr: pr.pop("platform"),
        lambda pr: pr.update(mode="bogus"),
    ],
)
def test_b171_provenance_schema_rejects(mutate):
    m = _manifest_ok()
    mutate(m["provenance"])
    with pytest.raises(cb.BundleValidationError):
        cb._validate_manifest(m)


def test_b187_official_requires_governed_markers():
    # B187/B199: mode=official sin los marcadores del run gobernado (git limpio + env_id + profile) se rechaza;
    # con TODOS pasa. Un git presente NO basta para 'official'.
    m = _manifest_ok()
    m["provenance"]["mode"] = "official"
    m["provenance"]["git_head"] = "a" * 40
    with pytest.raises(cb.BundleValidationError):  # falta env_id/git_tree/git_dirty=false/profile
        cb._validate_manifest(m)
    m["provenance"].update(git_tree="b" * 40, git_dirty=False, env_id=_H, profile="model")
    cb._validate_manifest(m)  # ahora sí (run gobernado completo)


# --------------------------------------------- B160/B172/B188: identidad + modo ---------------------------------------------


def test_b160_group_writable_output_rejected(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        bid = _commit(cfd, "tx.g")
        os.chmod(camp / ".merge-bundles" / bid / "outputs" / "campaign" / "campaign_pool_FAD_family.csv", 0o666)
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


def test_b188_exposed_bundles_root_blocks(tmp_path):
    # una raíz .merge-bundles preexistente en 0777 BLOQUEA (no se repara silenciosamente con fchmod).
    camp, cfd = _camp(tmp_path)
    try:
        os.mkdir(str(camp / ".merge-bundles"), 0o777)
        os.chmod(str(camp / ".merge-bundles"), 0o777)
        with pytest.raises(cb.BundleValidationError):
            _commit(cfd, "tx.x")
    finally:
        os.close(cfd)


# --------------------------------------------- B161/B173/B184: colisión + autoridad previa ---------------------------------------------


def test_b161_collision_corrupt_blocks(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        _commit(cfd, "tx.a")
        target = _commit(cfd, "tx.b", suffix="v2")
        before = cb._read_current(cfd)[1]
        p = camp / ".merge-bundles" / target / "outputs" / "campaign" / "campaign_pool_FAD_family.csv"
        os.chmod(p, 0o600)
        with open(p, "ab") as fh:
            fh.write(b"corrupt\n")
        with pytest.raises(cb.BundleValidationError):
            cb.build_and_commit(cfd, "tx.b", "campA", _outputs("v2"), _inputs(), _prov())
        assert cb._read_current(cfd)[1] == before
    finally:
        os.close(cfd)


def test_b173_invalid_prior_authority_blocks(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        a = _commit(cfd, "tx.a")
        before = cb._read_current(cfd)[1]
        p = camp / ".merge-bundles" / a / "outputs" / "campaign" / "campaign_pool_FAD_family.csv"
        os.chmod(p, 0o600)
        with open(p, "ab") as fh:
            fh.write(b"corrupt\n")
        with pytest.raises(cb.BundleValidationError):
            _commit(cfd, "tx.b", suffix="new")
        assert cb._read_current(cfd)[1] == before
    finally:
        os.close(cfd)


def test_b184_prior_corrupted_before_exchange_blocks(tmp_path, monkeypatch):
    # corromper el bundle previo DESPUÉS de la validación inicial (B173) y antes del exchange: la re-validación de
    # _cas_pointer (B184) lo caza y BLOQUEA; CURRENT no cambia.
    camp, cfd = _camp(tmp_path)
    try:
        a = _commit(cfd, "tx.a")
        before = cb._read_current(cfd)[1]
        real_write = cb._write_all
        done = {"x": False}

        def corrupting_write(fd, data):
            if not done["x"] and b"previous_bundle_id" in data:  # en la escritura del puntero temporal…
                done["x"] = True
                p = camp / ".merge-bundles" / a / "outputs" / "campaign" / "campaign_pool_FAD_family.csv"
                os.chmod(p, 0o600)
                with open(p, "ab") as fh:  # …corrompe el bundle previo (post-B173, pre-exchange)
                    fh.write(b"corrupt\n")
            return real_write(fd, data)

        monkeypatch.setattr(cb, "_write_all", corrupting_write)
        with pytest.raises(cb.BundleValidationError):
            _commit(cfd, "tx.b", suffix="new")
        monkeypatch.undo()
        assert cb._read_current(cfd)[1] == before
    finally:
        os.close(cfd)


# --------------------------------------------- B165/B175: handle inmutable ---------------------------------------------


def test_b165_fabricated_prepared_rejected(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        _commit(cfd, "tx.a")
        with pytest.raises(cb.BundleValidationError):
            cb._PreparedBundle(cfd, "f" * 64, "campA", _manifest_ok())
    finally:
        os.close(cfd)


def test_b175_campaign_id_is_readonly_from_manifest(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        with cb.prepare_bundle(cfd, "tx.a", "campA", _outputs(), _inputs(), _prov()) as prepared:
            assert prepared.campaign_id == "campA"
            with pytest.raises(AttributeError):  # propiedad de solo-lectura: no se puede mutar el puntero
                prepared.campaign_id = "evil"
            cb.commit_current(prepared)
        assert cb._read_current(cfd)[0]["campaign_id"] == "campA"  # el puntero conserva el del manifiesto
    finally:
        os.close(cfd)


def test_b175_single_use_handle(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        with cb.prepare_bundle(cfd, "tx.a", "campA", _outputs(), _inputs(), _prov()) as prepared:
            cb.commit_current(prepared)
            with pytest.raises(cb.BundleValidationError):  # segunda llamada rechazada
                cb.commit_current(prepared)
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
            with pytest.raises(cb.BundleValidationError):
                cb.commit_current(prepared)
    finally:
        os.close(cfd)


# --------------------------------------------- B176/B177/B181: primer CAS + nlink ---------------------------------------------


def test_b176_first_commit_substitution_never_valid_attacker(tmp_path, monkeypatch):
    # primer commit: sustituir el contenido de CURRENT (O_TRUNC) tras rename_noreplace NO deja una autoridad VÁLIDA
    # del atacante. El source-CAS detecta el contenido tampereado y lo PRESERVA (no lo retira); CURRENT queda
    # envenenado (apunta a un bundle inexistente) → no resuelve, y el commit eleva incompleto. Sin residuo `.tmp`.
    camp, cfd = _camp(tmp_path)
    try:
        with cb.prepare_bundle(cfd, "tx.a", "campA", _outputs(), _inputs(), _prov()) as prepared:
            real_rn = cb.rename_noreplace
            state = {"n": 0}

            def substituting(sfd, s, dfd, d):
                r = real_rn(sfd, s, dfd, d)
                if state["n"] == 0:
                    state["n"] = 1
                    fd = os.open(cb._CURRENT_NAME, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, dir_fd=cfd)
                    os.write(fd, b'{"schema_version":1,"campaign_id":"evil","bundle_id":"' + b"e" * 64 + b'","previous_bundle_id":null}')  # fmt: skip
                    os.fsync(fd)
                    os.close(fd)
                return r

            monkeypatch.setattr(cb, "rename_noreplace", substituting)
            with pytest.raises(cb.BundleError):
                cb.commit_current(prepared)
            monkeypatch.undo()
        with pytest.raises(cb.BundleError):  # CURRENT no resuelve a un bundle VÁLIDO (jamás autoridad atacante)
            cb.open_current_bundle(cfd)
        assert _residue(camp) == []  # sin `.merge-CURRENT.tmp` en la ruta oficial
    finally:
        os.close(cfd)


def test_b177_hardlinked_tmp_pointer_rejected(tmp_path, monkeypatch):
    # plantar un hardlink del puntero temporal (nlink=2) se detecta; CURRENT no se publica inválido.
    camp, cfd = _camp(tmp_path)
    try:
        with cb.prepare_bundle(cfd, "tx.a", "campA", _outputs(), _inputs(), _prov()) as prepared:
            real_fsync = cb.os.fsync
            state = {"done": False}

            def hardlinking_fsync(fd):
                if not state["done"]:  # exactamente UNA vez, en el primer fsync (el del tmp)
                    for n in os.listdir(cfd):
                        if n.startswith(".merge-CURRENT.tmp"):
                            state["done"] = True
                            os.link(n, n + ".hl", src_dir_fd=cfd, dst_dir_fd=cfd)
                            break
                return real_fsync(fd)

            monkeypatch.setattr(cb.os, "fsync", hardlinking_fsync)
            with pytest.raises(cb.BundleError):  # nlink!=1 → se detecta y se retira por cuarentena
                cb.commit_current(prepared)
            monkeypatch.undo()
        assert cb._read_current(cfd) is None  # no se publicó un CURRENT con nlink!=1
    finally:
        os.close(cfd)


def test_b181_first_cas_race_no_residue(tmp_path, monkeypatch):
    # si CURRENT aparece durante el rename_noreplace del primer commit, se eleva concurrencia SIN dejar .tmp.
    camp, cfd = _camp(tmp_path)
    try:
        with cb.prepare_bundle(cfd, "tx.a", "campA", _outputs(), _inputs(), _prov()) as prepared:
            real_rn = cb.rename_noreplace

            def racing(sfd, s, dfd, d):
                if d == cb._CURRENT_NAME:  # un tercero crea CURRENT antes de mi rename → FileExistsError
                    fd = os.open(cb._CURRENT_NAME, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=cfd)
                    os.write(fd, b'{"schema_version":1,"campaign_id":"other","bundle_id":"' + b"a" * 64 + b'","previous_bundle_id":null}')  # fmt: skip
                    os.close(fd)
                return real_rn(sfd, s, dfd, d)

            monkeypatch.setattr(cb, "rename_noreplace", racing)
            with pytest.raises(cb.BundleConcurrencyError):
                cb.commit_current(prepared)
            monkeypatch.undo()
        assert _residue(camp) == []
    finally:
        os.close(cfd)


# --------------------------------------------- B156/B182/B183: carrera, fsync, autoridad concurrente ---------------------------------------------


def test_b156_race_restores_concurrent(tmp_path, monkeypatch):
    camp, cfd = _camp(tmp_path)
    try:
        first = _commit(cfd, "tx.a")
        concurrent = cb._canon({"schema_version": 1, "campaign_id": "conc", "bundle_id": first, "previous_bundle_id": None})  # fmt: skip
        with cb.prepare_bundle(cfd, "tx.b", "campA", _outputs("b"), _inputs(), _prov()) as prepared:
            real_ex = cb.rename_exchange
            state = {"n": 0}

            def racing(sfd, s, dfd, d):
                if state["n"] == 0:
                    state["n"] = 1
                    fd = os.open(cb._CURRENT_NAME, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, dir_fd=cfd)
                    os.write(fd, concurrent)
                    os.fsync(fd)
                    os.close(fd)
                return real_ex(sfd, s, dfd, d)

            monkeypatch.setattr(cb, "rename_exchange", racing)
            with pytest.raises(cb.BundleConcurrencyError):
                cb.commit_current(prepared)
            monkeypatch.undo()
        assert cb._read_current(cfd)[1] == concurrent  # valor concurrente (a bundle VÁLIDO) restaurado
        assert _residue(camp) == []
    finally:
        os.close(cfd)


def test_b183_concurrent_authority_to_missing_bundle_is_incomplete(tmp_path, monkeypatch):
    # si el valor concurrente apunta a un bundle INEXISTENTE, la compensación lo detecta y eleva incompleto.
    camp, cfd = _camp(tmp_path)
    try:
        _commit(cfd, "tx.a")
        concurrent = cb._canon({"schema_version": 1, "campaign_id": "conc", "bundle_id": "f" * 64, "previous_bundle_id": None})  # fmt: skip
        with cb.prepare_bundle(cfd, "tx.b", "campA", _outputs("b"), _inputs(), _prov()) as prepared:
            real_ex = cb.rename_exchange
            state = {"n": 0}

            def racing(sfd, s, dfd, d):
                if state["n"] == 0:
                    state["n"] = 1
                    fd = os.open(cb._CURRENT_NAME, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, dir_fd=cfd)
                    os.write(fd, concurrent)
                    os.fsync(fd)
                    os.close(fd)
                return real_ex(sfd, s, dfd, d)

            monkeypatch.setattr(cb, "rename_exchange", racing)
            with pytest.raises(cb.BundleRollbackIncompleteError):
                cb.commit_current(prepared)
            monkeypatch.undo()
    finally:
        os.close(cfd)


def test_b182_postcas_fsync_failure_is_committed_state(tmp_path, monkeypatch):
    # un fsync(camp_fd) fallido DESPUÉS del CAS certificado escapa como CommittedStateError, no OSError crudo.
    camp, cfd = _camp(tmp_path)
    try:
        with cb.prepare_bundle(cfd, "tx.a", "campA", _outputs(), _inputs(), _prov()) as prepared:
            monkeypatch.setattr(cb, "_fsync_typed", lambda fd: (_ for _ in ()).throw(cb.CommittedStateError("fsync")))
            with pytest.raises(cb.CommittedStateError):
                cb.commit_current(prepared)
            monkeypatch.undo()
    finally:
        os.close(cfd)


# --------------------------------------------- B179/B180: cuarentena gobernada ---------------------------------------------


def test_b179_b191_quarantine_is_move_only_never_deletes():
    # B191: la cuarentena MOVE-ONLY jamás borra: mueve y PRESERVA. Un objeto que coincide con el lease queda
    # durable en la cuarentena (no unlink); uno mutado sobre el mismo inode se preserva como FOREIGN.
    import tempfile

    d = tempfile.mkdtemp()
    cfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fd = os.open("obj", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600, dir_fd=cfd)
        os.write(fd, b"content")
        lease = gf.OwnedLease(fd, is_dir=False)
        os.close(fd)
        with gf.GovernedQuarantine(cfd, "tx.move01") as q:
            dest = q.quarantine(cfd, "obj", lease)
        assert "obj" not in os.listdir(cfd)  # movido fuera de la ruta oficial
        quar = next(p for p in os.listdir(d) if p.startswith(".merge-quar"))
        assert dest in os.listdir(os.path.join(d, quar))  # PRESERVADO en cuarentena (move-only, no borrado)
    finally:
        os.close(cfd)


def test_b192_b207_same_inode_mutation_restored_to_official():
    # B192/B207 (source-CAS): una mutación de contenido sobre el mismo inode se detecta ANTES de retirar y el objeto
    # se RESTAURA a su ruta oficial (jamás se retira/pierde la actualización concurrente).
    import tempfile

    d = tempfile.mkdtemp()
    cfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fd = os.open("obj", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600, dir_fd=cfd)
        os.write(fd, b"orig")
        lease = gf.OwnedLease(fd, is_dir=False)
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, 0)
        os.write(fd, b"MUTATED-concurrent")  # mismo inode, contenido concurrente distinto
        os.close(fd)
        with gf.GovernedQuarantine(cfd, "tx.mut02") as q:
            with pytest.raises(gf.GovernedRemovalError):
                q.quarantine(cfd, "obj", lease)
        assert "obj" in os.listdir(cfd)  # RESTAURADO a su ruta oficial (source-CAS)
        assert open(os.path.join(d, "obj"), "rb").read() == b"MUTATED-concurrent"  # la actualización se conserva
    finally:
        os.close(cfd)


def test_b180_staging_rebound_to_foreign_preserved(tmp_path, monkeypatch):
    # si el nombre del staging se re-liga a un árbol ajeno antes de la limpieza, el árbol ajeno se PRESERVA.
    camp, cfd = _camp(tmp_path)
    try:
        foreign = camp / "foreign_tree"
        foreign.mkdir()
        (foreign / "sentinel").write_text("keep me")

        def failing_promote(camp_fd, staging_name, bundle_id, manifest):
            # simula un fallo de promoción y re-liga el NOMBRE del staging a un árbol AJENO (con su sentinel)
            os.rename(str(camp / staging_name), str(camp / "staging_gone"))
            os.rename(str(foreign), str(camp / staging_name))
            raise cb.BundleError("promoción simulada rota")

        monkeypatch.setattr(cb, "_promote_staging", failing_promote)
        with pytest.raises(cb.BundleError):  # move-only: el árbol ajeno se preserva, jamás se borra
            _commit(cfd, "tx.a")
        monkeypatch.undo()
        # el árbol ajeno NO fue borrado: su sentinel sobrevive (preservado en la cuarentena move-only)
        assert list(camp.rglob("sentinel")), "el árbol ajeno fue borrado (violación move-only)"
    finally:
        os.close(cfd)


# --------------------------------------------- B174: snapshot ---------------------------------------------


def test_b174_snapshot_consistent_across_swap(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        a = _commit(cfd, "tx.a")
        with cb.open_current_snapshot(cfd) as snap:
            first = snap.read("campaign", "campaign_pool_FAD_family.csv")
            b = _commit(cfd, "tx.b", suffix="NEW")
            assert a != b and snap.bundle_id == a
            second = snap.read("campaign", "campaign_pool_DFF_family.csv")
            assert first.startswith(_HDR.encode()) and second.startswith(_HDR.encode())
    finally:
        os.close(cfd)


def test_no_current_raises(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        with pytest.raises(cb.BundleError):
            cb.open_current_bundle(cfd)
    finally:
        os.close(cfd)


# --------------------------------------------- B189/B190/B197: certificación como unidad + compensación total ---------------------------------------------


def test_b189_current_swapped_during_certification_detected(tmp_path, monkeypatch):
    # cambiar CURRENT a OTRO bundle válido DENTRO de la certificación se detecta (CURRENT+bundle son una unidad).
    camp, cfd = _camp(tmp_path)
    try:
        other = _commit(cfd, "tx.other")  # existe un bundle válido alternativo
        with cb.prepare_bundle(cfd, "tx.a", "campA", _outputs("A"), _inputs(), _prov()) as prepared:
            real = cb._PreparedBundle.revalidate
            calls = {"n": 0}

            def swapping(self):
                calls["n"] += 1
                r = real(self)
                if calls["n"] == 2:  # dentro de _certify_current: swap CURRENT a `other`
                    ptr = cb._canon({"schema_version": 1, "campaign_id": "x", "bundle_id": other, "previous_bundle_id": None})  # fmt: skip
                    fd = os.open(cb._CURRENT_NAME, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, dir_fd=cfd)
                    os.write(fd, ptr)
                    os.fsync(fd)
                    os.close(fd)
                return r

            monkeypatch.setattr(cb._PreparedBundle, "revalidate", swapping)
            with pytest.raises(cb.BundleError):
                cb.commit_current(prepared)
            monkeypatch.undo()
    finally:
        os.close(cfd)


def test_b190_oserror_in_certify_compensates(tmp_path, monkeypatch):
    # un OSError (no BundleError) durante la certificación NO escapa crudo: compensa (CURRENT del primer commit se
    # retira) y re-eleva.
    camp, cfd = _camp(tmp_path)
    try:
        with cb.prepare_bundle(cfd, "tx.a", "campA", _outputs(), _inputs(), _prov()) as prepared:
            monkeypatch.setattr(cb, "_certify_current", lambda *a, **k: (_ for _ in ()).throw(OSError("crudo")))
            with pytest.raises(OSError):
                cb.commit_current(prepared)
            monkeypatch.undo()
        assert cb._read_current(cfd) is None  # CURRENT del primer commit se retiró → ausencia restaurada
    finally:
        os.close(cfd)


def test_b197_prior_corrupted_during_exchange_compensates(tmp_path, monkeypatch):
    # corromper el bundle previo DENTRO de rename_exchange (tras B184, en la linealización) → la certificación
    # (B197) revalida el previo, falla y compensa (restaura el previo desplazado); CURRENT no queda con autoridad rota.
    camp, cfd = _camp(tmp_path)
    try:
        a = _commit(cfd, "tx.a")
        before = cb._read_current(cfd)[1]
        real_ex = cb.rename_exchange
        done = {"x": False}

        def corrupting_exchange(sfd, s, dfd, d):
            if not done["x"]:
                done["x"] = True
                p = camp / ".merge-bundles" / a / "outputs" / "campaign" / "campaign_pool_FAD_family.csv"
                os.chmod(p, 0o600)
                with open(p, "ab") as fh:
                    fh.write(b"corrupt\n")
            return real_ex(sfd, s, dfd, d)

        monkeypatch.setattr(cb, "rename_exchange", corrupting_exchange)
        with pytest.raises(cb.BundleError):
            _commit(cfd, "tx.b", suffix="new")
        monkeypatch.undo()
        assert cb._read_current(cfd)[1] == before  # el previo desplazado fue restaurado como CURRENT
    finally:
        os.close(cfd)


# --------------------------------------------- B198/B200: contrato CSV anclado + journal durable ---------------------------------------------


def test_b198_mutated_contract_file_rejected(tmp_path, monkeypatch):
    # el contrato CSV se ancla por sha256 pineado: mutar el fichero en disco rompe la validación (caché no mutable).
    bad = tmp_path / "bad_contract.json"
    bad.write_text('{"encoding":"utf-8","columns":["a","b"]}')
    monkeypatch.setattr(cb, "_CSV_CONTRACT_PATH", str(bad))
    with pytest.raises(cb.BundleValidationError):
        cb._csv_columns()


def test_b198_manifest_carries_contract_sha(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        _commit(cfd, "tx.a")
        _, man = cb.open_current_bundle(cfd)
        assert man["provenance"]["csv_contract_sha256"] == cb._CSV_CONTRACT_SHA256
    finally:
        os.close(cfd)


def test_b200_quarantine_journal_tamper_detected(tmp_path):
    # el journal de la cuarentena se RELEE y valida su cadena tras cada append: un registro alterado se caza.
    import tempfile

    d = tempfile.mkdtemp()
    cfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fd = os.open("o", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600, dir_fd=cfd)
        os.write(fd, b"z")
        lease = gf.OwnedLease(fd, is_dir=False)
        os.close(fd)
        q = gf.GovernedQuarantine(cfd, "tx.j01")
        q.quarantine(cfd, "o", lease)
        jpath = os.path.join(d, q.name, "MANIFEST.jsonl")
        data = open(jpath, "rb").read().replace(b'"MOVED"', b'"HACKED"')
        open(jpath, "wb").write(data)
        with pytest.raises(gf.GovernedQuarantineError):
            q._reread_and_validate()
        q.close()
    finally:
        os.close(cfd)


# --------------------------------------------- B202/B204/B205/B207: source-CAS, journal, taxonomía ---------------------------------------------


def test_b202_journal_name_substitution_detected():
    # sustituir el NOMBRE MANIFEST.jsonl deja el fd huérfano; la próxima operación de journal lo caza (nombre↔inode).
    import tempfile

    d = tempfile.mkdtemp()
    cfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fd = os.open("o", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600, dir_fd=cfd)
        os.write(fd, b"z")
        lease = gf.OwnedLease(fd, is_dir=False)
        os.close(fd)
        q = gf.GovernedQuarantine(cfd, "tx.jsub")
        q.quarantine(cfd, "o", lease)  # crea el journal
        jdir = os.path.join(d, q.name)
        os.rename(os.path.join(jdir, "MANIFEST.jsonl"), os.path.join(jdir, "orphan"))
        open(os.path.join(jdir, "MANIFEST.jsonl"), "wb").write(b"FORGED\n")  # nombre re-ligado a un fichero ajeno
        fd2 = os.open("o2", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600, dir_fd=cfd)
        os.write(fd2, b"z")
        l2 = gf.OwnedLease(fd2, is_dir=False)
        os.close(fd2)
        with pytest.raises(gf.GovernedQuarantineError):  # el nombre no liga al fd del journal
            q.quarantine(cfd, "o2", l2)
        q.close()
    finally:
        os.close(cfd)


def test_b204_postcas_close_failure_is_committed_state(tmp_path, monkeypatch):
    # un error de cierre de la cuarentena DESPUÉS del CAS certificado se clasifica como CommittedStateError.
    camp, cfd = _camp(tmp_path)
    try:
        _commit(cfd, "tx.a")  # primer commit
        with cb.prepare_bundle(cfd, "tx.b", "campA", _outputs("b"), _inputs(), _prov()) as prepared:
            real_close = gf.GovernedQuarantine.close

            def failing_close(self):
                real_close(self)
                return ["fallo de cierre simulado"]  # el commit ya cruzó (previo desplazado en cuarentena)

            monkeypatch.setattr(gf.GovernedQuarantine, "close", failing_close)
            with pytest.raises(cb.CommittedStateError):
                cb.commit_current(prepared)
            monkeypatch.undo()
    finally:
        os.close(cfd)


def test_b205_staging_symlink_is_rollback_incomplete(tmp_path, monkeypatch):
    # sustituir el staging por un SYMLINK no se confunde con "ausente"; el source-CAS lo trata como ajeno e informa
    # incompleto (jamás silencia el symlink como si el staging hubiera desaparecido).
    camp, cfd = _camp(tmp_path)
    try:

        def sym_promote(camp_fd, staging_name, bundle_id, manifest):
            os.rename(str(camp / staging_name), str(camp / "staging_real"))
            os.symlink("staging_real", str(camp / staging_name))
            raise cb.BundleError("promoción rota")

        monkeypatch.setattr(cb, "_promote_staging", sym_promote)
        with pytest.raises(cb.BundleError):
            _commit(cfd, "tx.a")
        monkeypatch.undo()
        assert (camp / "staging_real").exists()  # el objeto ajeno no se perdió
    finally:
        os.close(cfd)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
