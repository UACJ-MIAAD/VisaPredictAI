"""M65-R1 · Contrato de `tools/check_latex_log.sh`, el gate de los `.log` de pdfLaTeX.

El gate no tenía ninguna prueba. Estas fijan lo que debe hacer con los logs REALES del
proyecto, que son **ISO-8859** y no UTF-8 (basta un acento: el de ProyectoI lleva siete
bytes `>0x7F` por «público»), y con lo que antes se le escapaba.

⚠️ **Corrección de lo que reporté en M65.** Dije que el gate era *fail-open* ante logs
ISO-8859 porque `grep` los declaraba binarios y devolvía un conteo vacío. **Era falso**:
lo hacía el `grep` de mi propia shell (una función que envuelve `ugrep` y se salta los
archivos binarios). El script se ejecuta con `bash`, que no hereda esa función y usa
`/usr/bin/grep`, y ése contaba bien. Lo que **sí** estaba fail-open, y aquí queda cerrado,
es que un log ausente —o un `grep` que falla al leer— dejaba los conteos vacíos, `[ "" -gt
0 ]` fallaba dentro de un `if` (exento de `set -e`) y el gate **salía 0**.

Por eso las pruebas van por comportamiento y no por el texto del script: se le da un log,
se mira su código de salida y su censo.
"""

from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]
GATE = RAIZ / "tools" / "check_latex_log.sh"

#: Bloque acentuado tal y como lo escribe pdfTeX: Latin-1, no UTF-8.
ACENTO = "[]|\\T1/ptm/m/n/10 (+20) Portal p\xfa-bli-co,\n"


def _log_iso8859(destino: pathlib.Path, cuerpo: str) -> pathlib.Path:
    """Escribe un log con la MISMA codificación que produce pdfTeX y lo comprueba."""
    destino.write_bytes((ACENTO + cuerpo).encode("latin-1"))
    with pytest.raises(UnicodeDecodeError):
        destino.read_bytes().decode("utf-8")  # el fixture es de verdad no-UTF-8
    return destino


def _correr(log: pathlib.Path | str, max_n: int = 0, max_pt: int = 0, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(GATE), str(log), str(max_n), str(max_pt)],
        capture_output=True,
        text=True,
        check=False,
        **kwargs,
    )


def _censo(salida: str) -> dict[str, str]:
    """Extrae los pares clave=valor de la línea de censo."""
    linea = next(ln for ln in salida.splitlines() if ln.startswith("censo "))
    pares = (t.split("=", 1) for t in linea.split() if "=" in t)
    return {k: v for k, v in pares}


def test_una_referencia_indefinida_en_un_log_iso8859_bloquea(tmp_path: pathlib.Path) -> None:
    log = _log_iso8859(
        tmp_path / "ref.log",
        "LaTeX Warning: Reference `tab:cohortes' on page 91 undefined on input line 3213.\n",
    )
    r = _correr(log)
    assert r.returncode == 1, r.stdout + r.stderr
    assert _censo(r.stdout)["undef_ref"] == "1"
    assert "referencias indefinidas" in r.stdout


def test_una_cita_indefinida_en_un_log_iso8859_bloquea(tmp_path: pathlib.Path) -> None:
    log = _log_iso8859(
        tmp_path / "cita.log",
        "LaTeX Warning: Citation `hyndman2021' on page 3 undefined on input line 42.\n",
    )
    r = _correr(log)
    assert r.returncode == 1, r.stdout + r.stderr
    assert _censo(r.stdout)["undef_cit"] == "1"


def test_un_overfull_sobre_el_umbral_en_un_log_iso8859_bloquea(tmp_path: pathlib.Path) -> None:
    log = _log_iso8859(
        tmp_path / "over.log",
        "Overfull \\hbox (42.5pt too wide) in paragraph at lines 10--12\n",
    )
    r = _correr(log)
    assert r.returncode == 1, r.stdout + r.stderr
    censo = _censo(r.stdout)
    assert censo["overfull_n"] == "1"
    assert censo["overfull_max"] == "42.5pt"


def test_un_log_iso8859_sin_coincidencias_se_acepta_con_el_censo_en_cero(tmp_path: pathlib.Path) -> None:
    log = _log_iso8859(
        tmp_path / "limpio.log",
        "Underfull \\hbox (badness 3040) in paragraph at lines 1--2\n"
        "Output written on doc.pdf (115 pages, 7105934 bytes).\n",
    )
    r = _correr(log)
    assert r.returncode == 0, r.stdout + r.stderr
    censo = _censo(r.stdout)
    # Los underfull son cosméticos y NO entran en el gate: el censo debe quedar en cero.
    assert [censo[k] for k in ("undef_ref", "undef_cit", "multiply", "rerun", "overfull_n")] == ["0"] * 5
    assert censo["overfull_max"] == "0pt"


def test_el_censo_solo_contiene_enteros_nunca_huecos(tmp_path: pathlib.Path) -> None:
    """Un conteo vacío es lo que hacía pasar al gate: el censo debe ser siempre numérico."""
    log = _log_iso8859(tmp_path / "censo.log", "nada que declarar\n")
    censo = _censo(_correr(log).stdout)
    for clave in ("undef_ref", "undef_cit", "multiply", "rerun", "overfull_n"):
        assert censo[clave].isdigit(), f"{clave} no es un entero: {censo[clave]!r}"


def test_un_log_ausente_es_un_fallo_del_gate(tmp_path: pathlib.Path) -> None:
    """Antes salía 0: `grep` protestaba, los conteos quedaban vacíos y nadie los miraba."""
    r = _correr(tmp_path / "no_existe.log")
    assert r.returncode != 0
    assert "no se puede leer el log" in r.stderr
    assert "censo " not in r.stdout, "no puede publicar un censo de un log que no leyó"


@pytest.mark.skipif(os.geteuid() == 0, reason="root lee igual un archivo sin permisos")
def test_un_log_ilegible_es_un_fallo_del_gate(tmp_path: pathlib.Path) -> None:
    log = _log_iso8859(tmp_path / "sinpermiso.log", "da igual lo que diga\n")
    log.chmod(0o000)
    try:
        r = _correr(log)
    finally:
        log.chmod(0o644)
    assert r.returncode != 0
    assert "censo " not in r.stdout


def test_un_grep_que_falla_al_leer_es_un_fallo_del_gate(tmp_path: pathlib.Path) -> None:
    """Error de lectura = fallo, no censo en cero.

    Se sustituye `grep` por uno que sale con 2 (el código de error de lectura). Antes, el
    `|| true` lo convertía en un conteo vacío y el gate seguía adelante hasta salir 0.
    """
    binario = tmp_path / "bin"
    binario.mkdir()
    (binario / "grep").write_text("#!/bin/sh\nexit 2\n")
    (binario / "grep").chmod(0o755)
    log = _log_iso8859(tmp_path / "ok.log", "un log perfectamente sano\n")

    entorno = dict(os.environ, PATH=f"{binario}{os.pathsep}{os.environ['PATH']}")
    r = _correr(log, env=entorno)
    assert r.returncode != 0, "un grep que no puede leer no puede leerse como 'sin hallazgos'"
    assert "error leyendo" in r.stderr


def test_sin_argumentos_falla_en_vez_de_dar_por_bueno_nada(tmp_path: pathlib.Path) -> None:
    r = subprocess.run(["bash", str(GATE)], capture_output=True, text=True, check=False)
    assert r.returncode != 0
    assert "uso:" in r.stderr


def test_los_umbrales_del_workflow_siguen_siendo_los_documentados() -> None:
    """El gate se aprieta al bajar, nunca se afloja en silencio (baseline del 11-jul-2026)."""
    wf = (RAIZ / ".github" / "workflows" / "latex.yml").read_text()
    assert "check_latex_log.sh reports/latex/ProyectoI_VisaPredictAI.log 0 0" in wf
    assert "check_latex_log.sh reports/latex/AnteproyectoVisaPredictAI.log 0 0" in wf
    assert "check_latex_log.sh reports/paper_micai/paper.log 3 15" in wf
