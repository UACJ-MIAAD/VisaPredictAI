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
    return cb._manifest_for(
        "campA", "tx.aaaa", None, ins, outs, _prov()
    )  # B230: previous_bundle_id sellado (None=inicial)


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
    # B235: un tercero escribe un puntero `concurrent` que comparte el bundle_id del previo pero con campaign_id
    # DISTINTO — NO es el predecesor CAPTURADO exacto. La reconciliación lo clasifica como AuthorityIndeterminate
    # (divergencia; requiere reconciliación humana), NUNCA como "restaurado al previo" rollback-seguro. El estado se
    # PRESERVA (la reconciliación jamás toca CURRENT): el valor concurrente sigue ahí.
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
            with pytest.raises(cb.AuthorityIndeterminateError):  # B235: divergencia del predecesor capturado
                cb.commit_current(prepared)
            monkeypatch.undo()
        assert cb._read_current(cfd)[1] == concurrent  # el valor concurrente se PRESERVA (indeterminado no toca nada)
        assert _residue(camp) == []
    finally:
        os.close(cfd)


def test_b183_concurrent_authority_to_missing_bundle_is_indeterminate(tmp_path, monkeypatch):
    # B221R: si un tercero deja CURRENT apuntando a un bundle INEXISTENTE (ajeno), la reconciliación fd-bound no puede
    # clasificarlo como previo ni nuevo → AuthorityIndeterminateError (reconciliación humana; sin rollback ni reintento).
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
            with pytest.raises(cb.AuthorityIndeterminateError):
                cb.commit_current(prepared)
            monkeypatch.undo()
    finally:
        os.close(cfd)


def test_b182_postcas_fsync_failure_is_typed(tmp_path, monkeypatch):
    # B221R: un fsync fallido DESPUÉS del CAS NO escapa como OSError crudo — la reconciliación fd-bound lo TIPA
    # (AuthorityIndeterminate: CURRENT cruzó pero no se pudo confirmar durable). Nunca un OSError suelto.
    camp, cfd = _camp(tmp_path)
    try:
        with cb.prepare_bundle(cfd, "tx.a", "campA", _outputs(), _inputs(), _prov()) as prepared:
            armed = {"x": False}
            real_fsync = os.fsync

            def failing(fd):
                if armed["x"]:
                    raise OSError("post-cas fsync")
                return real_fsync(fd)

            orig_cas = cb._cas_pointer

            def wrap_cas(*a, **k):  # arma el fallo justo antes del CAS: el próximo fsync es el post-CAS de durabilidad
                armed["x"] = True
                return orig_cas(*a, **k)

            monkeypatch.setattr(cb.os, "fsync", failing)
            monkeypatch.setattr(cb, "_cas_pointer", wrap_cas)
            with pytest.raises(cb.BundleError):  # TIPADO (BundleError), jamás un OSError crudo
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
    # B221R: un OSError (no BundleError) durante la certificación NO escapa crudo: se retira CURRENT (move-only) y la
    # reconciliación fd-bound (CURRENT ausente, CAS inicial) lo tipa como BundleError (no cruzó). CURRENT queda ausente.
    camp, cfd = _camp(tmp_path)
    try:
        with cb.prepare_bundle(cfd, "tx.a", "campA", _outputs(), _inputs(), _prov()) as prepared:
            monkeypatch.setattr(cb, "_certify_current", lambda *a, **k: (_ for _ in ()).throw(OSError("crudo")))
            with pytest.raises(cb.BundleError):  # tipado (no OSError crudo)
                cb.commit_current(prepared)
            monkeypatch.undo()
        assert cb._read_current(cfd) is None  # CURRENT del primer commit se retiró → ausencia restaurada (no cruzó)
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


# --------------------------------------------- B208/B209/B213/B215: ventanas y durabilidad de la cuarentena ---------------------------------------------


def _tmp_camp():
    import tempfile

    d = tempfile.mkdtemp()
    return d, os.open(d, os.O_RDONLY | os.O_DIRECTORY)


def test_b208_source_cas_placeholder_substitution(monkeypatch):
    # segunda ventana del source-CAS: sustituir el placeholder oficial tras el 1er exchange y antes del 2º move; el
    # objeto concurrente NO debe quedar desplazado con éxito → se detecta y restaura, elevando incompleto.
    d, cfd = _tmp_camp()
    try:
        fd = os.open("obj", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600, dir_fd=cfd)
        os.write(fd, b"mine")
        lease = gf.OwnedLease(fd, is_dir=False)
        os.close(fd)
        q = gf.GovernedQuarantine(cfd, "tx.b208")
        real_rn = gf.rename_noreplace
        state = {"n": 0}

        def racing(sfd, s, dfd, dd):
            if state["n"] == 0 and s == "obj":  # el 2º move (placeholder oficial → cuarentena)
                state["n"] = 1
                os.rename(os.path.join(d, "obj"), os.path.join(d, "ph_gone"))  # un tercero sustituye el placeholder
                cc = os.open("obj", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=cfd)
                os.write(cc, b"CONCURRENT")
                os.close(cc)
            return real_rn(sfd, s, dfd, dd)

        monkeypatch.setattr(gf, "rename_noreplace", racing)
        with pytest.raises(gf.GovernedRemovalError):
            q.quarantine(cfd, "obj", lease)
        monkeypatch.undo()
        assert "obj" in os.listdir(d)  # el objeto concurrente no desapareció (restaurado/preservado)
        q.close()
    finally:
        os.close(cfd)


def test_b209_quarantine_dir_rebind_detected():
    # religar el NOMBRE .merge-quar.* deja el fd huérfano; la siguiente operación lo caza (nombre↔inode del dir).
    d, cfd = _tmp_camp()
    try:
        fd = os.open("o", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600, dir_fd=cfd)
        os.write(fd, b"z")
        lease = gf.OwnedLease(fd, is_dir=False)
        os.close(fd)
        q = gf.GovernedQuarantine(cfd, "tx.b209")
        q.quarantine(cfd, "o", lease)  # crea el dir de cuarentena
        os.rename(os.path.join(d, q.name), os.path.join(d, "quar_gone"))  # religa el nombre a un dir ajeno
        os.mkdir(os.path.join(d, q.name), 0o700)
        fd2 = os.open("o2", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600, dir_fd=cfd)
        os.write(fd2, b"z")
        l2 = gf.OwnedLease(fd2, is_dir=False)
        os.close(fd2)
        with pytest.raises(gf.GovernedQuarantineError):
            q.quarantine(cfd, "o2", l2)
        q.close()
    finally:
        os.close(cfd)


def test_b213_fsync_failure_still_closes_fd(monkeypatch):
    # si fsync falla en close(), el fd SE CIERRA igualmente (no se fuga) y el error se reporta.
    d, cfd = _tmp_camp()
    try:
        fd = os.open("o", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600, dir_fd=cfd)
        os.write(fd, b"z")
        lease = gf.OwnedLease(fd, is_dir=False)
        os.close(fd)
        q = gf.GovernedQuarantine(cfd, "tx.b213")
        q.quarantine(cfd, "o", lease)
        jfd = q._jfd
        monkeypatch.setattr(gf.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("fsync roto")))
        errs = q.close()
        monkeypatch.undo()
        assert errs  # se reportó el fallo de fsync
        with pytest.raises(OSError):  # el fd YA fue cerrado (re-cerrar → EBADF): no se fugó
            os.close(jfd)
    finally:
        os.close(cfd)


def test_b215_journal_state_machine_rejects_bad_records():
    # el journal exige tipos exactos y máquina de estados: seq bool, terminal sin INTENT y record desconocido se cazan.
    import hashlib as _h

    d, cfd = _tmp_camp()
    try:
        fd = os.open("o", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600, dir_fd=cfd)
        os.write(fd, b"z")
        lease = gf.OwnedLease(fd, is_dir=False)
        os.close(fd)
        q = gf.GovernedQuarantine(cfd, "tx.b215")
        q.quarantine(cfd, "o", lease)
        jpath = os.path.join(d, q.name, "MANIFEST.jsonl")

        def rec(**kw):
            body = {"seq": kw["seq"], "record": kw["record"], "operation_id": kw["oid"], "dest": "o.x", "previous_record_sha256": ""}  # fmt: skip
            body["record_sha256"] = _h.sha256(gf._canon(body)).hexdigest()
            return gf._canon(body) + b"\n"

        for bad in (
            rec(seq=True, record="INTENT", oid="0" * 16),  # seq bool
            rec(seq=1, record="MOVED", oid="0" * 16),  # terminal sin INTENT
            rec(seq=1, record="WEIRD", oid="0" * 16),  # record desconocido
        ):
            open(jpath, "wb").write(bad)
            with pytest.raises(gf.GovernedQuarantineError):
                q._reread_and_validate()
        q.close()
    finally:
        os.close(cfd)


def test_b216_postcas_second_move_failure_is_committed_state(tmp_path, monkeypatch):
    # B216: si el 2º move (retiro del placeholder) del previo DESPLAZADO falla tras cruzar el CAS del bundle, el error
    # NO escapa crudo: se clasifica CommittedStateError; CURRENT ya es la autoridad nueva.
    camp, cfd = _camp(tmp_path)
    try:
        first = _commit(cfd, "tx.a")
        with cb.prepare_bundle(cfd, "tx.b", "campA", _outputs("b"), _inputs(), _prov()) as prepared:
            real = gf.rename_noreplace
            state = {"n": 0}

            def failing(sfd, s, dfd, dd):
                # el CAS del bundle usa rename_exchange; el 1er rename_noreplace es el retiro del placeholder del
                # previo desplazado (post-CAS) → inyectar el fallo ahí
                state["n"] += 1
                if state["n"] == 1:
                    raise OSError("INJECTED_SECOND_MOVE")
                return real(sfd, s, dfd, dd)

            monkeypatch.setattr(gf, "rename_noreplace", failing)
            with pytest.raises(cb.CommittedStateError):  # jamás OSError crudo
                cb.commit_current(prepared)
            monkeypatch.undo()
        assert cb._read_current(cfd)[0]["bundle_id"] != first  # el CAS cruzó: CURRENT es la autoridad nueva
    finally:
        os.close(cfd)


def _run_with_timeout(fn, timeout=5.0):
    import threading

    out = {}

    def run():
        try:
            fn()
            out["ok"] = True
        except BaseException as exc:  # noqa: BLE001 — capturamos el tipo para el aserto
            out["exc"] = type(exc).__name__

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    return (not t.is_alive()), out  # (terminó a tiempo, resultado)


@pytest.mark.parametrize("special", ["fifo", "socket"])
def test_b217_special_object_placeholder_no_hang(special):
    # B217: sustituir el placeholder oficial por un FIFO/socket tras el source-CAS NO debe COLGAR la transacción
    # (O_NONBLOCK) — debe terminar clasificando, no en un open() bloqueante infinito.
    import socket
    import tempfile

    d = tempfile.mkdtemp()
    cfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    sock = None
    try:
        fd = os.open("obj", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600, dir_fd=cfd)
        os.write(fd, b"mine")
        lease = gf.OwnedLease(fd, is_dir=False)
        os.close(fd)
        q = gf.GovernedQuarantine(cfd, "tx.b217")
        real = gf.rename_noreplace

        def substituting(sfd, s, dfd, dd):
            if s == "obj":  # tras el 1er exchange, un tercero sustituye el placeholder por un objeto ESPECIAL
                os.rename(os.path.join(d, "obj"), os.path.join(d, "gone"))
                if special == "fifo":
                    os.mkfifo(os.path.join(d, "obj"))
                else:
                    nonlocal sock
                    sock = socket.socket(socket.AF_UNIX)
                    sock.bind(os.path.join(d, "obj"))
            return real(sfd, s, dfd, dd)

        import unittest.mock as _m

        with _m.patch.object(gf, "rename_noreplace", substituting):
            finished, out = _run_with_timeout(lambda: q.quarantine(cfd, "obj", lease))
        assert finished, f"B217: la operación COLGÓ en un objeto {special} (open bloqueante)"
        assert out.get("exc") in ("GovernedRemovalError", "GovernedQuarantineIncompleteError")
        try:
            q.close()
        except gf.GovernedQuarantineError:
            pass
    finally:
        if sock is not None:
            sock.close()
        os.close(cfd)


def test_b217_lease_check_on_fifo_no_hang():
    # el chequeo de lease sobre un FIFO no bloquea y no coincide (nunca se lee su contenido).
    import tempfile

    d = tempfile.mkdtemp()
    cfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fd = os.open("real", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600, dir_fd=cfd)
        os.write(fd, b"x")
        lease = gf.OwnedLease(fd, is_dir=False)
        os.close(fd)
        q = gf.GovernedQuarantine(cfd, "tx.b217b")
        q._ensure()
        os.mkfifo("fifo_slot", dir_fd=q.fd)
        holder: list = []
        finished, _out = _run_with_timeout(lambda: holder.append(q._lease_matches("fifo_slot", lease)))
        assert finished and holder[-1] is False  # no cuelga, no coincide
        q.close()
    finally:
        os.close(cfd)


def _run_isolated(code, timeout=6.0):
    """Ejecuta `code` en un SUBPROCESO killable (NO un thread daemon: un open bloqueante sobre un FIFO no se puede
    interrumpir dentro del mismo proceso). Devuelve (terminó_a_tiempo, stdout+stderr)."""
    import subprocess
    import sys as _sys

    p = subprocess.Popen(
        [_sys.executable, "-c", code], cwd=_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    try:
        out, _ = p.communicate(timeout=timeout)
        return True, out
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
        return False, "<TIMEOUT>"


_B218_JOURNAL_SPECIAL = """
import os, sys, tempfile
sys.path.insert(0, os.getcwd())
import tools.governed_fs as gf
d = tempfile.mkdtemp(dir="/tmp")  # ruta corta: un socket AF_UNIX topa el límite de 104 bytes de sun_path
cfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
q = gf.GovernedQuarantine(cfd, "tx.b218")
fd = os.open("o1", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600, dir_fd=cfd); os.write(fd, b"a")
l1 = gf.OwnedLease(fd, is_dir=False); os.close(fd)
q.quarantine(cfd, "o1", l1)                       # 1ª operación: crea el dir de cuarentena + journal
man = os.path.join(d, q.name, "MANIFEST.jsonl")
os.unlink(man)
if {kind!r} == "fifo":
    os.mkfifo(man)                                # un tercero del mismo UID sustituye el journal por un FIFO
else:
    import socket
    s = socket.socket(socket.AF_UNIX); s.bind(man)
fd2 = os.open("o2", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600, dir_fd=cfd); os.write(fd2, b"b")
l2 = gf.OwnedLease(fd2, is_dir=False); os.close(fd2)
try:                                              # 2ª operación: _journal(INTENT) -> _bind_all -> reabre MANIFEST.jsonl
    q.quarantine(cfd, "o2", l2)                   # VIEJO(FIFO): CUELGA en el open O_RDONLY; NUEVO: error de dominio
    print("NO_ERROR")
except gf.GovernedQuarantineError as e:
    print("CLASSIFIED:" + type(e).__name__)
print("DONE")
"""


@pytest.mark.parametrize("kind", ["fifo", "socket"])
def test_b218_journal_special_object_no_hang_post_source_cas(kind):
    # B218: la reapertura por NOMBRE de MANIFEST.jsonl en _bind_all() debe ser no bloqueante — un FIFO sustituido no
    # debe COLGAR la transacción tras el source-CAS (subproceso killable, no thread daemon).
    completed, out = _run_isolated(_B218_JOURNAL_SPECIAL.replace("{kind!r}", repr(kind)))
    assert completed, f"B218: la reapertura del journal COLGÓ sobre un {kind}"
    assert "DONE" in out and "CLASSIFIED" in out, out


def _journal_ops(quar_dir):
    """Lee MANIFEST.jsonl y agrupa por operation_id → {oid: [records...]}."""
    man = os.path.join(quar_dir, "MANIFEST.jsonl")
    by_op: dict = {}
    for line in open(man).read().splitlines():
        rec = json.loads(line)
        by_op.setdefault(rec["operation_id"], []).append(rec["record"])
    return by_op


def test_b220_special_object_writes_incomplete_terminal(monkeypatch):
    # B220: si el placeholder oficial es sustituido por un FIFO tras el source-CAS, la operación NO puede terminar con
    # un INTENT colgante — debe journalizar EXACTAMENTE un terminal INCOMPLETE, preservar el objeto y no dar éxito.
    d, cfd = _tmp_camp()
    try:
        fd = os.open("obj", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600, dir_fd=cfd)
        os.write(fd, b"mine")
        lease = gf.OwnedLease(fd, is_dir=False)
        os.close(fd)
        q = gf.GovernedQuarantine(cfd, "tx.b220")
        real_rn = gf.rename_noreplace
        fired = {"n": 0}

        def racing(sfd, s, dfd, dd):
            if fired["n"] == 0 and s == "obj":  # el 2º move (placeholder oficial → cuarentena)
                fired["n"] = 1
                os.unlink(os.path.join(d, "obj"))
                os.mkfifo(os.path.join(d, "obj"))  # un tercero sustituye el placeholder por un FIFO
            return real_rn(sfd, s, dfd, dd)

        monkeypatch.setattr(gf, "rename_noreplace", racing)
        with pytest.raises((gf.GovernedRemovalError, gf.GovernedQuarantineIncompleteError)):
            q.quarantine(cfd, "obj", lease)
        monkeypatch.undo()
        by_op = _journal_ops(os.path.join(d, q.name))
        for ops in by_op.values():  # cada INTENT tiene EXACTAMENTE un terminal (sin colgantes, sin dobles)
            assert ops.count("INTENT") == 1
            terminals = [x for x in ops if x in ("MOVED", "FOREIGN_PRESERVED", "ABSENT", "INCOMPLETE")]
            assert len(terminals) == 1, f"operación sin terminal único: {ops}"
        assert any("INCOMPLETE" in ops for ops in by_op.values()), "la operación sustituida debe terminar en INCOMPLETE"
        import stat as _st

        quar = os.path.join(d, q.name)  # el objeto especial (FIFO) se PRESERVA en la cuarentena, no se borra
        assert any(_st.S_ISFIFO(os.lstat(os.path.join(quar, n)).st_mode) for n in os.listdir(quar)), (
            "el objeto especial (FIFO) debe preservarse en la cuarentena"
        )
        q.close()
    finally:
        os.close(cfd)


@pytest.mark.parametrize("phase", ["pre_cas", "post_cert"])
def test_b220_taxonomy_special_placeholder(phase, tmp_path):
    # B220/taxonomía: un objeto especial en la cuarentena de un puntero se clasifica según la fase —
    # pre-CAS → BundleRollbackIncompleteError; post-certificado → CommittedStateError.
    camp, cfd = _camp(tmp_path)
    try:
        with gf.GovernedQuarantine(cfd, f"tx.tax.{phase}") as q:
            tmp_fd = os.open("ptr.tmp", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600, dir_fd=cfd)
            os.write(tmp_fd, b"P")
            real_rx = gf.rename_exchange
            fired = {"n": 0}

            def racing(sfd, s, dfd, dd):
                if fired["n"] == 0 and s == "ptr.tmp":  # tras el 1er exchange, sustituir el placeholder por un FIFO
                    fired["n"] = 1
                    r = real_rx(sfd, s, dfd, dd)
                    os.unlink(os.path.join(str(camp), "ptr.tmp"))
                    os.mkfifo(os.path.join(str(camp), "ptr.tmp"))
                    return r
                return real_rx(sfd, s, dfd, dd)

            import unittest.mock as _m

            fn = cb._quarantine_pointer if phase == "pre_cas" else cb._quarantine_pointer_prev
            # B221R: pre-CAS clasifica a BundleRollbackIncompleteError; post-cert `_quarantine_pointer_prev` PROPAGA
            # el error gobernado (el llamador `_cas_pointer` lo tipa como CommittedStateError CON el certificado).
            expected = cb.BundleRollbackIncompleteError if phase == "pre_cas" else (gf.GovernedRemovalError, gf.GovernedQuarantineError)  # fmt: skip
            with _m.patch.object(gf, "rename_exchange", racing):
                with pytest.raises(expected):
                    fn(q, cfd, "ptr.tmp", tmp_fd, b"P")
            try:
                os.close(tmp_fd)
            except OSError:
                pass
    finally:
        os.close(cfd)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_b225_reconcile_reverifies_current_binding(tmp_path, monkeypatch):
    # B225: la reconciliacion RE-VERIFICA inode+bytes de CURRENT tras validar+fsync; una mutacion en sitio en esa
    # ventana (evidencia obsoleta) -> AuthorityIndeterminate, jamas un certificado obsoleto.
    camp, cfd = _camp(tmp_path)
    try:
        _commit(cfd, "tx.a")  # CURRENT = bundle A
        real_fsync = os.fsync
        fired = {"n": 0}

        def fsync_mutate(fd):
            if fired["n"] == 0:  # muta CURRENT en sitio (mismo inode, bytes distintos) durante el fsync de reconcile
                fired["n"] = 1
                mfd = os.open(cb._CURRENT_NAME, os.O_RDWR | os.O_NOFOLLOW, dir_fd=cfd)
                data = b""
                while chunk := os.read(mfd, 4096):
                    data += chunk
                os.lseek(mfd, 0, os.SEEK_SET)
                os.write(mfd, data + b" ")
                os.close(mfd)
            return real_fsync(fd)

        with cb.prepare_bundle(cfd, "tx.a", "campA", _outputs(), _inputs(), _prov()) as prepared:
            # prepared.bundle_id == A (mismo txid+outputs) == CURRENT.bundle_id -> rama CURRENT==nuevo
            ident = cb._read_current(cfd)[2]
            monkeypatch.setattr(cb.os, "fsync", fsync_mutate)
            with pytest.raises(cb.AuthorityIndeterminateError):
                cb._reconcile_and_raise(cfd, prepared, ident, None, cb.BundleValidationError("primary"), "test")
            monkeypatch.undo()
    finally:
        os.close(cfd)


def test_b232_pointer_matches_enforces_governance(tmp_path):
    # B232: la re-verificacion final exige las MISMAS invariantes gobernadas que la lectura inicial (nlink==1, modo
    # 0600); un hardlink o un cambio de modo tras la primera lectura hace que _pointer_matches deje de coincidir.
    camp, cfd = _camp(tmp_path)
    try:
        name = "p"
        content = b'{"x":1}'
        fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=cfd)
        os.write(fd, content)
        os.close(fd)
        ident = cb._ident(os.stat(name, dir_fd=cfd, follow_symlinks=False))
        assert cb._pointer_matches(cfd, name, ident, content)  # gobernado OK (regular, UID, nlink==1, 0600)
        os.link(name, "p.hardlink", src_dir_fd=cfd, dst_dir_fd=cfd)  # nlink -> 2
        assert not cb._pointer_matches(cfd, name, ident, content)  # B232: hardlink adicional NO coincide
        os.unlink("p.hardlink", dir_fd=cfd)
        assert cb._pointer_matches(cfd, name, ident, content)  # restaurado
        os.chmod(name, 0o666, dir_fd=cfd)  # modo 0600 -> 0666
        assert not cb._pointer_matches(cfd, name, ident, content)  # B232: modo != 0600 NO coincide
    finally:
        os.close(cfd)


def test_b230_concurrent_predecessor_change_aborts(tmp_path):
    # B230: si un tercero cambia CURRENT entre prepare y commit, el CAS exige el predecesor CAPTURADO+SELLADO exacto
    # (bundle_id+bytes+inode) y ABORTA sin reutilizar el manifiesto. Cierra la ventana prepare->commit.
    camp, cfd = _camp(tmp_path)
    try:
        _commit(cfd, "tx.a")  # CURRENT = A
        prepared = cb.prepare_bundle(cfd, "tx.b", "campA", _outputs("b"), _inputs(), _prov())  # sella previous = A
        try:
            _commit(cfd, "tx.c", suffix="c")  # tercero: CURRENT A -> C (predecesor cambia)
            with pytest.raises(cb.BundleConcurrencyError):
                cb.commit_current(prepared)
        finally:
            prepared.close()
    finally:
        os.close(cfd)


def test_b235_prev_branch_requires_exact_captured_predecessor(tmp_path):
    # B235: en la reconciliacion, la rama cur_bid==prev_id declara NOT_CROSSED (BundleConcurrencyError) SOLO si CURRENT
    # es EXACTAMENTE el predecesor capturado. Un puntero con el mismo bundle_id pero campaign_id/bytes forjados ->
    # AuthorityIndeterminateError (no rollback-seguro).
    camp, cfd = _camp(tmp_path)
    try:
        a = _commit(cfd, "tx.a")  # CURRENT = A (predecesor)
        prepared = cb.prepare_bundle(cfd, "tx.b", "campA", _outputs("b"), _inputs(), _prov())  # captura A
        try:
            # caso POSITIVO: CURRENT sigue siendo el A capturado exacto -> NOT_CROSSED
            with pytest.raises(cb.BundleConcurrencyError):
                cb._reconcile_and_raise(cfd, prepared, prepared._ident, a, cb.BundleValidationError("primary"), "test")
            # caso FORJADO: mismo bundle_id A, campaign_id distinto (bytes/inode nuevos) -> INDETERMINADO
            real = json.loads(cb._read_current(cfd)[1])
            forged = {**real, "campaign_id": "FORGED"}
            os.unlink(cb._CURRENT_NAME, dir_fd=cfd)
            fd = os.open(cb._CURRENT_NAME, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=cfd)
            os.write(fd, cb._canon(forged))
            os.close(fd)
            with pytest.raises(cb.AuthorityIndeterminateError):
                cb._reconcile_and_raise(cfd, prepared, prepared._ident, a, cb.BundleValidationError("primary"), "test")
        finally:
            prepared.close()
    finally:
        os.close(cfd)


def test_b230_lineage_chain_walk(tmp_path):
    # B230: validate_current_report RECORRE y valida la cadena COMPLETA de ancestros (C->B->A->None). Un ancestro
    # alterado o inexistente ROMPE la validacion; campanas distintas entre eslabones son legitimas.
    camp, cfd = _camp(tmp_path)
    try:
        a = _commit(cfd, "tx.a")  # A (previous=None)
        _commit(cfd, "tx.b", suffix="b")  # B (previous=A)
        _commit(cfd, "tx.c", suffix="c")  # C (previous=B); CURRENT=C
        rep = cb.validate_current_report(cfd)
        assert rep["status"] == "valid" and rep["lineage_depth"] == 2  # dos ancestros (B, A)
        # ancestro ALTERADO: mutar el manifiesto de A -> su bundle_id ya no == sha(manifest) -> cadena rota
        man = camp / ".merge-bundles" / a / "manifest.json"
        os.chmod(man, 0o600)
        data = json.loads(man.read_text())
        data["txid"] = str(data["txid"]) + "X"
        man.write_text(json.dumps(data))
        with pytest.raises(cb.BundleError):
            cb.validate_current_report(cfd)
    finally:
        os.close(cfd)


def test_b230_lineage_missing_ancestor(tmp_path):
    # B230: un ancestro INEXISTENTE (bundle dir borrado) rompe el recorrido de linaje.
    camp, cfd = _camp(tmp_path)
    try:
        a = _commit(cfd, "tx.a")
        _commit(cfd, "tx.b", suffix="b")  # CURRENT=B, previous=A
        import shutil as _sh

        _sh.rmtree(camp / ".merge-bundles" / a)  # borra el ancestro A
        with pytest.raises(cb.BundleError):
            cb.validate_current_report(cfd)
    finally:
        os.close(cfd)


def test_b230_lineage_different_campaigns_ok(tmp_path):
    # B230: campanas DISTINTAS encadenan legitimamente (la campana puede cambiar entre eslabones).
    camp, cfd = _camp(tmp_path)
    try:
        cb.build_and_commit(cfd, "tx.a", "campA", _outputs("a"), _inputs(), _prov())
        cb.build_and_commit(cfd, "tx.b", "campB", _outputs("b"), _inputs(), _prov())  # campana distinta sobre campA
        rep = cb.validate_current_report(cfd)
        assert rep["status"] == "valid" and rep["campaign_id"] == "campB" and rep["lineage_depth"] == 1
    finally:
        os.close(cfd)


def test_b241_no_fd_leak_on_repeated_lineage_failure(tmp_path):
    # B241: validaciones de linaje que FALLAN repetidamente NO fugan descriptores (el fd se registra al abrir).
    camp, cfd = _camp(tmp_path)
    try:
        a = _commit(cfd, "tx.a")
        _commit(cfd, "tx.b", suffix="b")  # CURRENT=B, previous=A
        man = camp / ".merge-bundles" / a / "manifest.json"
        os.chmod(man, 0o600)
        d = json.loads(man.read_text())
        d["txid"] = str(d["txid"]) + "X"
        man.write_text(json.dumps(d))  # A alterado -> el recorrido de linaje falla
        before = len(os.listdir("/dev/fd"))
        for _ in range(50):
            with pytest.raises(cb.BundleError):
                cb.validate_current_report(cfd)
        assert len(os.listdir("/dev/fd")) - before <= 1, "fuga de descriptores en el recorrido fallido"
    finally:
        os.close(cfd)


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_b240_authority_lock_shared_exclusive(tmp_path):
    # B240: el lock de autoridad da instantanea entre procesos COOPERATIVOS: EX excluye SH, dos SH coexisten.
    import fcntl as _f

    camp = tmp_path / "reports" / "campaign"
    camp.mkdir(parents=True)
    cfd = os.open(str(camp), os.O_RDONLY | os.O_DIRECTORY)
    try:
        with cb._authority_lock(cfd, exclusive=False):  # crea el lock file gobernado
            pass

        def child_holds(exclusive):
            held_r, held_w = os.pipe()
            rel_r, rel_w = os.pipe()
            pid = os.fork()
            if pid == 0:  # hijo: toma el lock, senala, espera release
                os.close(held_r)
                os.close(rel_w)
                f = os.open(cb._AUTHORITY_LOCK_NAME, os.O_RDWR | os.O_NOFOLLOW, dir_fd=cfd)
                _f.flock(f, _f.LOCK_EX if exclusive else _f.LOCK_SH)
                os.write(held_w, b"1")
                os.read(rel_r, 1)
                os._exit(0)
            os.close(held_w)
            os.close(rel_r)
            os.read(held_r, 1)  # espera "held"
            return pid, rel_w, held_r

        pid, rel_w, held_r = child_holds(True)  # 1. EX en el hijo excluye SH en el padre
        f = os.open(cb._AUTHORITY_LOCK_NAME, os.O_RDWR | os.O_NOFOLLOW, dir_fd=cfd)
        try:
            with pytest.raises(BlockingIOError):
                _f.flock(f, _f.LOCK_SH | _f.LOCK_NB)
        finally:
            os.close(f)
            os.write(rel_w, b"1")
            os.close(rel_w)
            os.close(held_r)
            os.waitpid(pid, 0)

        pid, rel_w, held_r = child_holds(False)  # 2. dos SH coexisten
        f = os.open(cb._AUTHORITY_LOCK_NAME, os.O_RDWR | os.O_NOFOLLOW, dir_fd=cfd)
        try:
            _f.flock(f, _f.LOCK_SH | _f.LOCK_NB)  # sin BlockingIOError
            _f.flock(f, _f.LOCK_UN)
        finally:
            os.close(f)
            os.write(rel_w, b"1")
            os.close(rel_w)
            os.close(held_r)
            os.waitpid(pid, 0)
    finally:
        os.close(cfd)


def test_b240_lock_governance_fail_closed(tmp_path):
    # B240 (robo/recreacion del inode del lock): un lock file no gobernado (modo != 0600) hace fail-closed.
    camp = tmp_path / "reports" / "campaign"
    camp.mkdir(parents=True)
    cfd = os.open(str(camp), os.O_RDONLY | os.O_DIRECTORY)
    try:
        f = os.open(cb._AUTHORITY_LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=cfd)
        os.close(f)
        os.chmod(cb._AUTHORITY_LOCK_NAME, 0o666, dir_fd=cfd)  # modo ajeno -> no gobernado
        with pytest.raises(cb.BundleValidationError):
            with cb._authority_lock(cfd, exclusive=False):
                pass
    finally:
        os.close(cfd)


def test_b244_lock_inode_rebind_detected(tmp_path):
    # B244: si el NOMBRE del lock se re-liga a un inode NUEVO tras adquirirlo, reverify() lo caza (fail-closed) —
    # de lo contrario otro proceso flockearia el inode nuevo simultaneamente y se romperia la exclusion.
    camp = tmp_path / "reports" / "campaign"
    camp.mkdir(parents=True)
    cfd = os.open(str(camp), os.O_RDONLY | os.O_DIRECTORY)
    try:
        with cb._authority_lock(cfd, exclusive=True) as lk:
            lk.reverify()  # ok: el inode flockeado es el del nombre
            os.rename(cb._AUTHORITY_LOCK_NAME, cb._AUTHORITY_LOCK_NAME + ".old", src_dir_fd=cfd, dst_dir_fd=cfd)
            fd2 = os.open(
                cb._AUTHORITY_LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=cfd
            )
            os.close(fd2)  # el nombre apunta ahora a un inode NUEVO
            with pytest.raises(cb.BundleValidationError):
                lk.reverify()
    finally:
        os.close(cfd)


def test_b246_lineage_digest_reproducible_and_policy_cap(tmp_path, monkeypatch):
    # B246/2R7: el digest de linaje es LOGICO (solo bundle_ids ordenados, SIN inodes) -> reproducible; el tope es de
    # POLITICA (1024, O(1) fds).
    assert cb._POLICY_MAX_LINEAGE == 1024 and not hasattr(cb, "_effective_lineage_max")
    camp, cfd = _camp(tmp_path)
    try:
        _commit(cfd, "tx.a")
        _commit(cfd, "tx.b", suffix="b")
        _commit(cfd, "tx.c", suffix="c")  # cadena C->B->A
        r1 = cb.validate_current_report(cfd)
        r2 = cb.validate_current_report(cfd)
        assert r1["lineage_depth"] == 2
        assert r1["lineage_evidence_digest"] == r2["lineage_evidence_digest"]  # reproducible entre corridas
        monkeypatch.setattr(cb, "_POLICY_MAX_LINEAGE", 1)
        with pytest.raises(cb.BundleValidationError):  # 2 > 1 -> rechazado
            cb.validate_current_report(cfd)
    finally:
        os.close(cfd)


def test_b248_post_cas_lock_rebind_is_committed_not_rollback(tmp_path, monkeypatch):
    # B248 (critico): si el lock se re-liga DESPUES de que _run_commit cruzo CURRENT durable, commit_current NO debe
    # elevar un BundleValidationError pre-CAS (que dispararia rollback) — debe RECONCILIAR y elevar CommittedStateError
    # con el certificado real. CURRENT queda cruzado al bundle nuevo.
    camp, cfd = _camp(tmp_path)
    try:
        _commit(cfd, "tx.a")  # CURRENT=A; crea el lock file
        real = cb._run_commit

        def hooked(camp_fd, quar, prepared):
            cert = real(camp_fd, quar, prepared)  # CAS real -> CURRENT cruza a B
            os.rename(cb._AUTHORITY_LOCK_NAME, cb._AUTHORITY_LOCK_NAME + ".x", src_dir_fd=camp_fd, dst_dir_fd=camp_fd)
            fd = os.open(
                cb._AUTHORITY_LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=camp_fd
            )
            os.close(fd)  # rebind del inode del lock DESPUES del cruce
            return cert

        monkeypatch.setattr(cb, "_run_commit", hooked)
        prepared = cb.prepare_bundle(cfd, "tx.b", "campA", _outputs("b"), _inputs(), _prov())
        bexp = prepared.bundle_id
        with pytest.raises(cb.CommittedStateError) as ei:
            cb.commit_current(prepared)
        assert ei.value.certificate.bundle_id == bexp  # el cert de la autoridad CRUZADA
        assert cb._read_current(cfd)[0]["bundle_id"] == bexp  # CURRENT quedo cruzado a B (sin rollback)
    finally:
        os.close(cfd)
