"""B332: identidad de imports por DESCRIPTOR gobernado (tools/governed_import_identity).

En el SHA base `deep_smoke.identity_problems` sólo comparaba `spec.origin` como texto (`realpath`+`startswith`), así que
un `__spec__.origin` FORJADO/inexistente bajo `sys.prefix` se ACEPTABA sin abrir nada. Aquí, con paquetes temporales
GOBERNADOS bajo un prefijo de prueba, se exige que el origin exista, sea regular, no-symlink en toda la cadena, `nlink==1`,
sin escritura grupo/otros, del uid actual, y pertenezca a los ficheros declarados por la distribución; se hashea desde el
descriptor. Casos: válido, inexistente/forjado, symlink de leaf, symlink de componente, hardlink, 0666, origin ajeno,
proveedor incorrecto, RECORD ausente, no-inventariado y namespace (origin None)."""

from __future__ import annotations

import os

import pytest

import tools.governed_import_identity as gi


@pytest.fixture
def pkg(tmp_path):
    """Crea `prefix/lib/pkgx/__init__.py` (0644) bajo un prefijo de prueba y devuelve `(prefix, origin, dist_files)`."""
    prefix = tmp_path / "prefix"
    d = prefix / "lib" / "pkgx"
    d.mkdir(parents=True)
    origin = d / "__init__.py"
    origin.write_text("# pkgx\n")
    origin.chmod(0o644)
    return str(prefix), str(origin), [str(origin)]


def test_valid_module_certifies_with_sha(pkg):
    prefix, origin, files = pkg
    probs, ident = gi.governed_identity("pkgx", "pkgx", origin=origin, providing=["pkgx"], dist_files=files, sys_prefix=prefix)  # fmt: skip
    assert probs == [], probs
    assert ident is not None and ident.module == "pkgx" and ident.distribution == "pkgx"
    assert ident.origin == "lib/pkgx/__init__.py" and ident.origin_sha256.startswith("sha256:")


def test_forged_nonexistent_origin_is_rejected(pkg):
    # EL AGUJERO DE B332: un origin bajo el prefijo pero INEXISTENTE. En el string-only pasaba; aquí no abre → problema.
    prefix, origin, files = pkg
    forged = os.path.join(prefix, "lib", "pkgx", "__FORGED__.py")
    probs, ident = gi.governed_identity("pkgx", "pkgx", origin=forged, providing=["pkgx"], dist_files=files, sys_prefix=prefix)  # fmt: skip
    assert ident is None and any("no abrible" in p for p in probs), probs


def test_leaf_symlink_is_rejected(pkg, tmp_path):
    prefix, origin, _files = pkg
    target = tmp_path / "outside.py"
    target.write_text("x\n")
    link = os.path.join(prefix, "lib", "pkgx", "evil.py")
    os.symlink(str(target), link)
    probs, ident = gi.governed_identity("pkgx", "pkgx", origin=link, providing=["pkgx"], dist_files=[link], sys_prefix=prefix)  # fmt: skip
    assert ident is None and any("no abrible sin seguir symlink" in p for p in probs), probs


def test_component_symlink_is_rejected(pkg, tmp_path):
    prefix, origin, files = pkg
    os.symlink(os.path.join(prefix, "lib"), os.path.join(prefix, "liblink"))  # liblink -> lib
    via_link = os.path.join(prefix, "liblink", "pkgx", "__init__.py")
    probs, ident = gi.governed_identity("pkgx", "pkgx", origin=via_link, providing=["pkgx"], dist_files=files, sys_prefix=prefix)  # fmt: skip
    assert ident is None and any("no-symlink" in p for p in probs), probs


def test_hardlink_is_rejected(pkg):
    prefix, origin, _files = pkg
    hard = os.path.join(prefix, "lib", "pkgx", "hard.py")
    os.link(origin, hard)  # nlink del inode == 2
    probs, ident = gi.governed_identity("pkgx", "pkgx", origin=hard, providing=["pkgx"], dist_files=[hard], sys_prefix=prefix)  # fmt: skip
    assert ident is None and any("nlink" in p for p in probs), probs


def test_group_other_writable_is_rejected(pkg):
    prefix, origin, files = pkg
    os.chmod(origin, 0o666)
    probs, ident = gi.governed_identity("pkgx", "pkgx", origin=origin, providing=["pkgx"], dist_files=files, sys_prefix=prefix)  # fmt: skip
    assert ident is None and any("escribible por grupo/otros" in p for p in probs), probs


def test_foreign_origin_outside_prefix_is_rejected(pkg):
    prefix, _origin, _files = pkg
    probs, ident = gi.governed_identity("pkgx", "pkgx", origin="/etc/hosts", providing=["pkgx"], dist_files=["/etc/hosts"], sys_prefix=prefix)  # fmt: skip
    assert ident is None and any("fuera de sys.prefix" in p for p in probs), probs


def test_wrong_provider_is_rejected(pkg):
    prefix, origin, files = pkg
    probs, ident = gi.governed_identity("pkgx", "pkgx", origin=origin, providing=["evil"], dist_files=files, sys_prefix=prefix)  # fmt: skip
    assert ident is None and any("packages_distributions" in p for p in probs), probs


def test_missing_record_is_rejected(pkg):
    prefix, origin, _files = pkg
    probs, ident = gi.governed_identity("pkgx", "pkgx", origin=origin, providing=["pkgx"], dist_files=None, sys_prefix=prefix)  # fmt: skip
    assert ident is None and any("RECORD ausente" in p for p in probs), probs


def test_origin_not_in_distribution_files_is_rejected(pkg, tmp_path):
    prefix, origin, _files = pkg
    other = tmp_path / "other.py"
    other.write_text("y\n")
    probs, ident = gi.governed_identity("pkgx", "pkgx", origin=origin, providing=["pkgx"], dist_files=[str(other)], sys_prefix=prefix)  # fmt: skip
    assert ident is None and any("no pertenece a los ficheros" in p for p in probs), probs


def test_namespace_origin_none_is_rejected(pkg):
    prefix, _origin, files = pkg
    probs, ident = gi.governed_identity("pkgx", "pkgx", origin=None, providing=["pkgx"], dist_files=files, sys_prefix=prefix)  # fmt: skip
    assert ident is None and any("sin origin certificable" in p for p in probs), probs


def test_relative_origin_is_rejected(pkg):
    prefix, _origin, files = pkg
    probs, ident = gi.governed_identity("pkgx", "pkgx", origin="lib/pkgx/__init__.py", providing=["pkgx"], dist_files=files, sys_prefix=prefix)  # fmt: skip
    assert ident is None and any("no es una ruta absoluta" in p for p in probs), probs
