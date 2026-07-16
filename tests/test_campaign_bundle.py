"""Bundle inmutable content-addressed + puntero CURRENT por CAS (P0R.5 · R9.2R12 · B148/B145). El bundle es la
AUTORIDAD del commit; los consumidores resuelven vía `open_current_bundle()`/`read_current_csv()`."""

from __future__ import annotations

import hashlib
import os

import pytest

import tools.campaign_bundle as cb


def _camp(tmp_path):
    camp = tmp_path / "reports" / "campaign"
    camp.mkdir(parents=True)
    return camp, os.open(str(camp), os.O_RDONLY | os.O_DIRECTORY)


def _outputs(suffix=b""):
    return [
        {
            "label": "campaign",
            "name": "campaign_pool_FAD_family.csv",
            "bytes": b"a,b\n1,2\n" + suffix,
            "rows": 1,
            "cols": 2,
        },  # fmt: skip
        {"label": "eval", "name": "model_comparison_FAD21.csv", "bytes": b"x,y\n3,4\n" + suffix, "rows": 1, "cols": 2},
    ]


_INPUTS = [{"name": "aq_pool_nongbm_FAD_family.csv", "size": 2, "sha256": hashlib.sha256(b"in").hexdigest()}]
_PROV = {"git_head": None, "python": "3.14"}


def test_build_commit_resolve(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        bid = cb.build_and_commit(cfd, "txA.deadbeef", "campA", _outputs(), _INPUTS, _PROV)
        assert len(bid) == 64
        rid, manifest = cb.open_current_bundle(cfd)
        assert rid == bid and len(manifest["outputs"]) == 2
        assert cb.read_current_csv(cfd, "campaign", "campaign_pool_FAD_family.csv") == b"a,b\n1,2\n"
    finally:
        os.close(cfd)


def test_bundle_id_is_content_addressed(tmp_path):
    # el mismo contenido → el mismo bundle_id (determinista); contenido distinto → id distinto.
    camp, cfd = _camp(tmp_path)
    try:
        b1 = cb.build_and_commit(cfd, "tx1.aaaa", "campA", _outputs(), _INPUTS, _PROV)
        b2 = cb.build_and_commit(cfd, "tx2.bbbb", "campA", _outputs(b"z\n"), _INPUTS, _PROV)
        assert b1 != b2
        cur = cb._read_current(cfd)
        assert cur["bundle_id"] == b2 and cur["previous_bundle_id"] == b1  # CAS: swap con previo verificado
    finally:
        os.close(cfd)


def test_tampered_sealed_output_detected(tmp_path):
    # corromper un output SELLADO dentro del bundle inmutable → validate_bundle/open_current_bundle lo cazan.
    camp, cfd = _camp(tmp_path)
    try:
        bid = cb.build_and_commit(cfd, "txT.cccc", "campA", _outputs(), _INPUTS, _PROV)
        sealed = camp / ".merge-bundles" / bid / "outputs" / "campaign" / "campaign_pool_FAD_family.csv"
        with open(sealed, "ab") as fh:
            fh.write(b"TAMPER\n")
        with pytest.raises(cb.BundleError):
            cb.open_current_bundle(cfd)
        with pytest.raises(cb.BundleError):
            cb.read_current_csv(cfd, "campaign", "campaign_pool_FAD_family.csv")
    finally:
        os.close(cfd)


def test_tampered_manifest_detected(tmp_path):
    # alterar el manifiesto sellado → bundle_id != sha256(manifest) → rechazado.
    camp, cfd = _camp(tmp_path)
    try:
        bid = cb.build_and_commit(cfd, "txM.dddd", "campA", _outputs(), _INPUTS, _PROV)
        manifest = camp / ".merge-bundles" / bid / "manifest.json"
        data = manifest.read_bytes().replace(b'"campA"', b'"HACKED"')
        manifest.write_bytes(data)
        with pytest.raises(cb.BundleError):
            cb.open_current_bundle(cfd)
    finally:
        os.close(cfd)


def test_current_hardlink_rejected(tmp_path):
    # CURRENT con nlink=2 (hardlink) se rechaza — no basta el contenido, se exige identidad gobernada.
    camp, cfd = _camp(tmp_path)
    try:
        cb.build_and_commit(cfd, "txH.eeee", "campA", _outputs(), _INPUTS, _PROV)
        os.link(str(camp / ".merge-CURRENT"), str(camp / ".merge-CURRENT.hardlink"))  # nlink → 2
        with pytest.raises(cb.BundleError):
            cb.open_current_bundle(cfd)
    finally:
        os.close(cfd)


def test_no_current_raises(tmp_path):
    camp, cfd = _camp(tmp_path)
    try:
        with pytest.raises(cb.BundleError):
            cb.open_current_bundle(cfd)
    finally:
        os.close(cfd)


def test_immutable_bundle_collision_blocks_on_diff(tmp_path):
    # un bundle preexistente con el mismo id (mismo txid+contenido+inputs+procedencia) pero SELLADO distinto
    # bloquea (content-addressed inmutable: nunca se reescribe un bundle sellado).
    camp, cfd = _camp(tmp_path)
    try:
        bid = cb.build_and_commit(cfd, "txC.ffff", "campA", _outputs(), _INPUTS, _PROV)
        (camp / ".merge-bundles" / bid / "manifest.json").write_bytes(b"{}")  # corrompe el bundle sellado
        with pytest.raises(cb.BundleError):  # re-commit del MISMO id (mismo txid) → colisión, difiere → bloquea
            cb.build_and_commit(cfd, "txC.ffff", "campA", _outputs(), _INPUTS, _PROV)
    finally:
        os.close(cfd)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
