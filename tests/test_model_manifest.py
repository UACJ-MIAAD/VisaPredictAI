"""Inventario EXACTO + identidad de modelos (auditoria 13-jul-2026 ronda 9).

Fixtures con identidades UNICAS reales (artefactos distintos por clave), no 260x el mismo
diccionario. Cubre los casos adversariales del paso 6: SHA viejo (local y deep), panel_hash
distinto, hash sintacticamente valido pero incorrecto, git_dirty ausente/string, duplicados
de clave y de path, global_fake (extra), artefacto modificado tras el manifiesto, y faltante.
"""

from __future__ import annotations

from pathlib import Path

from tools import model_manifest as mm
from tools.campaign_hashing import artifact_tree_sha256

CAMP = {
    "campaign_id": "rederiv_deadbee_20260713",
    "source_git_sha": "a" * 40,
    "git_dirty": False,
    "panel_sha256": "sha256:" + "p" * 64,
}


def _ok(root: Path, *, typ, table, model, country=None, category=None, content=None):
    rel = f"models/{table}/{typ}/{model}" + (f"__{country}_{category}" if country else "")
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content or f"{typ}-{table}-{model}-{country}-{category}")  # identidad unica real
    e = {
        "schema_version": mm.SCHEMA_VERSION,
        "campaign_id": CAMP["campaign_id"],
        "source_git_sha": CAMP["source_git_sha"],
        "git_dirty": CAMP["git_dirty"],
        "panel_sha256": CAMP["panel_sha256"],
        "type": typ,
        "table": table,
        "model": model,
        "status": "ok",
        "path": rel,
        "artifact_sha256": artifact_tree_sha256(p),
        "created_at": "2026-07-13T00:00:00",
    }
    if country:
        e["country"], e["category"] = country, category
    return e


def _fixture(root: Path):
    """Un inventario exacto minimo: 1 global + 2 locales (identidades distintas)."""
    g = _ok(root, typ="global_deep", table="FAD", model="BiTCN")
    l1 = _ok(root, typ="local", table="FAD", model="ets", country="mexico", category="F1")
    l2 = _ok(root, typ="local", table="FAD", model="theta", country="india", category="F4")
    entries = [g, l1, l2]
    expected = {mm.semantic_key(e) for e in entries}
    return entries, expected


def _run(root, entries, expected):
    return mm.validate_inventory(entries, expected=expected, campaign=CAMP, root=root)


def test_exact_valid_inventory_passes(tmp_path):
    entries, expected = _fixture(tmp_path)
    assert _run(tmp_path, entries, expected) == []


def test_local_old_sha_fails(tmp_path):
    entries, expected = _fixture(tmp_path)
    entries[1]["source_git_sha"] = "b" * 40  # local de otra campana
    assert any("source_git_sha" in p for p in _run(tmp_path, entries, expected))


def test_deep_old_sha_fails(tmp_path):
    entries, expected = _fixture(tmp_path)
    entries[0]["source_git_sha"] = "c" * 40  # deep de otra campana
    assert any("source_git_sha" in p for p in _run(tmp_path, entries, expected))


def test_panel_hash_mismatch_fails(tmp_path):
    entries, expected = _fixture(tmp_path)
    entries[2]["panel_sha256"] = "sha256:" + "q" * 64
    assert any("panel_sha256" in p for p in _run(tmp_path, entries, expected))


def test_wrong_artifact_hash_fails(tmp_path):
    entries, expected = _fixture(tmp_path)
    entries[0]["artifact_sha256"] = "sha256:" + "0" * 64  # sintacticamente valido, incorrecto
    assert any("artifact_sha256" in p for p in _run(tmp_path, entries, expected))


def test_git_dirty_missing_fails(tmp_path):
    entries, expected = _fixture(tmp_path)
    del entries[1]["git_dirty"]
    assert any("git_dirty" in p or "faltan campos" in p for p in _run(tmp_path, entries, expected))


def test_git_dirty_string_fails(tmp_path):
    entries, expected = _fixture(tmp_path)
    entries[1]["git_dirty"] = "false"  # string, no booleano
    assert any("git_dirty" in p for p in _run(tmp_path, entries, expected))


def test_duplicate_semantic_key_fails(tmp_path):
    entries, expected = _fixture(tmp_path)
    dup = dict(entries[1])  # misma clave semantica que l1
    entries.append(dup)
    assert any("clave semantica duplicada" in p for p in _run(tmp_path, entries, expected))


def test_duplicate_path_fails(tmp_path):
    entries, expected = _fixture(tmp_path)
    # otra clave semantica pero apuntando al MISMO path (inflar conteo)
    clone = _ok(tmp_path, typ="local", table="FAD", model="kalman", country="china", category="F2A")
    clone["path"] = entries[1]["path"]
    clone["artifact_sha256"] = entries[1]["artifact_sha256"]
    entries.append(clone)
    expected = expected | {mm.semantic_key(clone)}
    assert any("path duplicado" in p for p in _run(tmp_path, entries, expected))


def test_unexpected_key_fails(tmp_path):
    entries, expected = _fixture(tmp_path)
    fake = _ok(tmp_path, typ="global_deep", table="FAD", model="FAKE")  # global_fake, no esperado
    entries.append(fake)  # expected NO lo incluye
    assert any("INESPERADAS" in p for p in _run(tmp_path, entries, expected))


def test_artifact_modified_after_manifest_fails(tmp_path):
    entries, expected = _fixture(tmp_path)
    # el artefacto cambia DESPUES de escribir el manifiesto -> recompute != registrado
    (tmp_path / entries[0]["path"]).write_text("weights v2 (alterado)")
    assert any("artifact_sha256" in p for p in _run(tmp_path, entries, expected))


def test_missing_expected_key_fails(tmp_path):
    entries, expected = _fixture(tmp_path)
    dropped = entries.pop()  # nunca se intento: falta en el manifiesto
    assert mm.semantic_key(dropped) in expected
    assert any("FALTANTES" in p for p in _run(tmp_path, entries, expected))


def test_failed_entry_explains_absence(tmp_path):
    entries, expected = _fixture(tmp_path)
    # en vez de 'ok', el deep declara un fallo LEGITIMO con motivo -> inventario completo
    entries[0] = {
        "schema_version": mm.SCHEMA_VERSION,
        "campaign_id": CAMP["campaign_id"],
        "source_git_sha": CAMP["source_git_sha"],
        "git_dirty": CAMP["git_dirty"],
        "panel_sha256": CAMP["panel_sha256"],
        "type": "global_deep",
        "table": "FAD",
        "model": "BiTCN",
        "status": "failed",
        "error_type": "RuntimeError",
        "reason": "no convergio en 800 pasos",
    }
    assert _run(tmp_path, entries, expected) == []


def test_failed_entry_without_reason_fails(tmp_path):
    entries, expected = _fixture(tmp_path)
    entries[0] = {
        "schema_version": mm.SCHEMA_VERSION,
        "campaign_id": CAMP["campaign_id"],
        "source_git_sha": CAMP["source_git_sha"],
        "git_dirty": CAMP["git_dirty"],
        "panel_sha256": CAMP["panel_sha256"],
        "type": "global_deep",
        "table": "FAD",
        "model": "BiTCN",
        "status": "failed",  # sin error_type/reason
    }
    assert any("sin error_type/reason" in p for p in _run(tmp_path, entries, expected))


def test_path_escape_fails(tmp_path):
    entries, expected = _fixture(tmp_path)
    entries[1]["path"] = "../../etc/passwd"
    assert any("escapa del repo" in p for p in _run(tmp_path, entries, expected))
