"""``pipeline.ingest_manual`` (A2): la vía manual mientras la fuente está tras
Cloudflare, con las MISMAS validaciones que protegen a ``data/snapshots/`` (la
copia maestra S3) de un HTML envenenado: nombre↔mes, marcadores de boletín,
smoke de parseo con el piso de completitud del gate de ingesta
(``REQUIRED_COMBOS`` / ``MIN_ROWS_NEW_MONTH``) y snapshot INMUTABLE: el
re-ingest byte-idéntico es no-op, el conflicto aborta sin escape (la
sustitución excepcional queda fuera de alcance). Contrato de stdout: la ÚLTIMA
línea es la ruta destino (la usa ``make ingest-manual`` para el put a S3, que
es create-only). El encadenado del Makefile también se prueba: un fallo del
validador DEBE detener la cadena antes de tocar S3.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from pipeline import ingest_manual
from pipeline.scrape_visa_bulletins import is_employment_section
from vp_data import config

ROOT = Path(__file__).resolve().parent.parent

FIXTURES = Path(__file__).parent / "fixtures"
MODERN = FIXTURES / "vb_2022_06.html"  # boletín moderno real: 4 combos bloque×tabla
ARCHAIC = FIXTURES / "vb_2007_06.html"  # pre-Oct-2015: solo FAD (sin DFF)


@pytest.fixture()
def snap_dir(tmp_path, monkeypatch):
    d = tmp_path / "snapshots"
    d.mkdir()
    monkeypatch.setattr(config, "SNAPSHOTS_DIR", d)
    return d


def _run(*argv: str) -> Path:
    return ingest_manual.main(list(argv))


# ------------------------------------------------------------------ name / month
def test_unmappable_name_without_month_is_rejected(snap_dir, tmp_path):
    f = tmp_path / "descarga(3).html"
    f.write_bytes(MODERN.read_bytes())
    with pytest.raises(SystemExit, match="--month"):
        _run(str(f))


def test_month_flag_disagreeing_with_a_mappable_name_is_rejected(snap_dir, tmp_path):
    f = tmp_path / "visa-bulletin-for-june-2022.html"
    f.write_bytes(MODERN.read_bytes())
    with pytest.raises(SystemExit, match="no coincide"):
        _run(str(f), "--month", "2026-08")


def test_bad_month_format_is_rejected(snap_dir, tmp_path):
    f = tmp_path / "x.html"
    f.write_bytes(MODERN.read_bytes())
    with pytest.raises(SystemExit, match="YYYY-MM"):
        _run(str(f), "--month", "agosto-2026")


# ----------------------------------------------------------------------- content
def test_non_bulletin_content_is_rejected(snap_dir, tmp_path):
    f = tmp_path / "visa-bulletin-for-august-2026.html"
    f.write_bytes(b"<html><title>Access Denied</title>Reference #18</html>")
    with pytest.raises(SystemExit, match="marcadores"):
        _run(str(f))
    assert not list(snap_dir.iterdir())  # nada llegó a snapshots


def test_family_only_bulletin_is_rejected_by_the_completeness_floor(snap_dir, tmp_path):
    # boletín moderno real al que se le AMPUTA la sección de empleo: el smoke de
    # parseo debe exigir las 4 combinaciones bloque×tabla, como el gate K3
    soup = BeautifulSoup(MODERN.read_text(encoding="utf-8", errors="replace"), "html.parser")
    removed = 0
    for table in soup.find_all("table"):
        if is_employment_section(table.find_all("tr")):
            table.decompose()
            removed += 1
    assert removed, "el fixture moderno debe traer tablas de empleo que amputar"
    f = tmp_path / "visa-bulletin-for-june-2022.html"
    f.write_bytes(str(soup).encode())
    with pytest.raises(SystemExit, match="employment"):
        _run(str(f))


def test_pre2015_bulletin_without_dff_is_rejected(snap_dir, tmp_path):
    f = tmp_path / "visa-bulletin-for-june-2007.html"
    f.write_bytes(ARCHAIC.read_bytes())
    with pytest.raises(SystemExit, match="DFF"):
        _run(str(f))


# ---------------------------------------------------------------------- success
def test_modern_bulletin_ingests_with_month_flag_and_prints_dest_last(snap_dir, tmp_path, capsys):
    f = tmp_path / "descarga.html"  # nombre del navegador, sin mes mapeable
    f.write_bytes(MODERN.read_bytes())
    dest = _run(str(f), "--month", "2022-06")
    assert dest == snap_dir / "visa-bulletin-for-june-2022.html"
    assert dest.read_bytes() == MODERN.read_bytes()
    assert not list(snap_dir.glob("*.part"))  # copia atómica: sin restos
    out = capsys.readouterr().out.strip().splitlines()
    assert out[-1] == str(dest)  # contrato del Makefile: última línea = destino


def test_identical_duplicate_is_a_noop(snap_dir, tmp_path, capsys):
    f = tmp_path / "visa-bulletin-for-june-2022.html"
    f.write_bytes(MODERN.read_bytes())
    dest = _run(str(f))
    capsys.readouterr()
    assert _run(str(f)) == dest  # byte-idéntico: no-op, NO error
    assert dest.read_bytes() == MODERN.read_bytes()
    out = capsys.readouterr().out.strip().splitlines()
    assert out[-1] == str(dest)  # el contrato de stdout (última línea = destino) se mantiene


def test_conflicting_duplicate_aborts_with_no_escape(snap_dir, tmp_path):
    f = tmp_path / "visa-bulletin-for-june-2022.html"
    f.write_bytes(MODERN.read_bytes())
    dest = _run(str(f))
    dest.write_bytes(b"tampered")  # simula un snapshot previo DISTINTO
    with pytest.raises(SystemExit, match="inmutable"):
        _run(str(f))
    assert dest.read_bytes() == b"tampered"  # nadie toca el congelado


def test_force_flag_does_not_exist(snap_dir, tmp_path):
    # la sustitución excepcional queda FUERA de alcance en esta tajada: sin escape
    f = tmp_path / "visa-bulletin-for-june-2022.html"
    f.write_bytes(MODERN.read_bytes())
    with pytest.raises(SystemExit) as exc:
        _run(str(f), "--force")
    assert exc.value.code == 2  # argparse: argumento desconocido


def test_force_is_not_advertised_anywhere_in_the_module():
    # guard: ni el docstring ni la ayuda del CLI pueden volver a anunciar el
    # escape que esta tajada eliminó (residuo cazado en auditoría del autor)
    src = (ROOT / "pipeline" / "ingest_manual.py").read_text(encoding="utf-8")
    assert "--force" not in src, "pipeline/ingest_manual.py anuncia --force (flag inexistente)"


# ------------------------------------------------------- make ingest-manual chain
def _make_env(shim_dir: Path) -> dict[str, str]:
    return {**os.environ, "PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}"}


def _aws_shim(tmp_path: Path) -> tuple[Path, Path]:
    """Un `aws` falso primero en el PATH: registra que fue invocado y sale 0.
    Así el test prueba la CADENA del Makefile sin credenciales ni S3 real."""
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    marker = tmp_path / "aws_was_called"
    shim = shim_dir / "aws"
    shim.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n")
    shim.chmod(0o755)
    return shim_dir, marker


def test_make_ingest_manual_stops_on_validator_failure_before_s3(tmp_path):
    # un no-boletín con nombre mapeable: el validador sale 1 y la cadena DEBE
    # detenerse ahí — ni aws (shim) ni el rebuild llegan a ejecutarse
    bad = tmp_path / "visa-bulletin-for-august-2026.html"
    bad.write_bytes(b"<html><title>Access Denied</title>Reference #18</html>")
    shim_dir, marker = _aws_shim(tmp_path)
    proc = subprocess.run(
        # MAKE=true neutraliza el rebuild pesado ($(MAKE) scrape panel db) por si
        # una regresión deja pasar la cadena: el test sigue detectándola (rc=0 +
        # marker del shim) sin re-ejecutar el pipeline real dentro de un test.
        ["make", "ingest-manual", f"PY={sys.executable}", f"FILE={bad}", "MAKE=true"],
        cwd=ROOT,
        env=_make_env(shim_dir),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode != 0, f"el fallo del validador se tragó: rc=0\nstdout:{proc.stdout}\nstderr:{proc.stderr}"
    assert not marker.exists(), "aws se invocó pese al fallo del validador"


def test_make_recipe_uploads_create_only_and_is_phony():
    text = (ROOT / "Makefile").read_text()
    recipe = text.split("ingest-manual:", 1)[1].split("\n\n", 1)[0]
    assert "s3api put-object" in recipe and "--if-none-match" in recipe, "el put a S3 debe ser create-only"
    assert "aws s3 cp" not in recipe, "aws s3 cp puede SOBRESCRIBIR la copia maestra"
    phony = next(line for line in text.splitlines() if line.startswith(".PHONY"))
    assert "ingest-manual" in phony


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "--no-cov"]))
