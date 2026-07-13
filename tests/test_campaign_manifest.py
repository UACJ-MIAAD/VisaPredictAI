"""Tests conductuales del contrato de publicabilidad (auditoría 13-jul-2026 ronda 8).

Ejercen los casos EXACTOS que el ``grep '"dirty": *true'`` dejaba pasar (fail-open):
manifiesto ausente/vacío/malformado, ``{"dirty" : true}`` con espacios, sin la clave,
``dirty`` como string/entero. Solo el booleano JSON ``false`` autoriza publicar.
"""

from __future__ import annotations

import json

import tools.campaign_manifest as cm


def _w(tmp_path, text):
    p = tmp_path / "campaign_manifest.json"
    p.write_text(text)
    return p


def test_clean_false_is_publishable(tmp_path):
    p = _w(tmp_path, json.dumps({"campaign_id": "c", "git_sha": "abc", "dirty": False}))
    assert cm.publish_blocker(p) is None


def test_missing_manifest_blocks(tmp_path):
    assert cm.publish_blocker(tmp_path / "no_existe.json")


def test_empty_file_blocks(tmp_path):
    assert cm.publish_blocker(_w(tmp_path, ""))


def test_malformed_json_blocks(tmp_path):
    assert cm.publish_blocker(_w(tmp_path, "{no es json"))


def test_missing_dirty_key_blocks(tmp_path):
    assert cm.publish_blocker(_w(tmp_path, json.dumps({"campaign_id": "c"})))


def test_dirty_true_blocks(tmp_path):
    assert cm.publish_blocker(_w(tmp_path, json.dumps({"dirty": True})))


def test_dirty_true_with_spaces_blocks(tmp_path):
    # {"dirty" : true} — el grep '"dirty": *true' NO lo veía (espacio antes de los dos puntos)
    assert cm.publish_blocker(_w(tmp_path, '{"dirty" : true}'))


def test_dirty_string_false_blocks(tmp_path):
    # "false" (string) no es el booleano False
    assert cm.publish_blocker(_w(tmp_path, json.dumps({"dirty": "false"})))


def test_dirty_zero_blocks(tmp_path):
    # 0 == False pero `is not False` lo rechaza (no es un booleano JSON)
    assert cm.publish_blocker(_w(tmp_path, json.dumps({"dirty": 0})))


def test_dirty_null_blocks(tmp_path):
    assert cm.publish_blocker(_w(tmp_path, json.dumps({"dirty": None})))


def test_non_object_blocks(tmp_path):
    assert cm.publish_blocker(_w(tmp_path, json.dumps([1, 2, 3])))


def test_cli_exit_code(tmp_path):
    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps({"dirty": False}))
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"dirty": True}))
    assert cm.main(["prog", "--assert-publishable", str(ok)]) == 0
    assert cm.main(["prog", "--assert-publishable", str(bad)]) == 7
