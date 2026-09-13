"""M74-E · El entorno que va a correr la campaña tiene que REPRODUCIR su lock.

El preflight hasheaba `locks/model-cpu.txt` y `locks/deep-macos-arm64.txt` y comprobaba que los
directorios `ante/` y `ante_nf/` **existieran**. Dicho de otro modo: sellaba la *declaración* del
entorno y jamás el entorno. Medido el 13-sep-2026 sobre los intérpretes reales:

    ante     vs locks/model-cpu.txt        →  3 pines ausentes, 24 a otra versión
    ante_nf  vs locks/deep-macos-arm64.txt → 18 pines ausentes, 32 a otra versión

con **`torch` 2.13.0 en el lock contra 2.12.0 instalado** en ambos, más `numba`/`llvmlite` —que
compilan al vuelo el núcleo de statsforecast— y `coreforecast`. Once horas de cálculo habrían dado
cifras que `locks/` no reconstruye, y el recibo las habría llamado selladas.

Las pruebas son **herméticas**: montan intérpretes de mentira (un `bin/python` que imprime un censo
fijo) para poder ejercitar el acuerdo y el desacuerdo sin depender de qué haya instalado hoy.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from tools import check_env_matches_lock as el  # noqa: E402


def _venv_falso(base: Path, nombre: str, distribuciones: dict[str, str]) -> Path:
    """Un intérprete que responde el censo que le digamos. No instala nada ni toca la red."""
    venv = base / nombre
    (venv / "bin").mkdir(parents=True)
    py = venv / "bin" / "python"
    py.write_text(
        "#!/bin/sh\ncat <<'EOF'\n" + json.dumps(distribuciones) + "\nEOF\n",
        encoding="utf-8",
    )
    py.chmod(py.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return venv


def _lock(base: Path, nombre: str, pines: dict[str, str]) -> Path:
    p = base / nombre
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# generado\n" + "\n".join(f"{k}=={v}" for k, v in pines.items()) + "\n", encoding="utf-8")
    return p


# ═══════════════════════════════ 1 · el criterio
def test_un_entorno_identico_al_lock_reproduce(tmp_path: Path) -> None:
    v = _venv_falso(tmp_path, "ante", {"torch": "2.13.0", "numba": "0.66.0"})
    lk = _lock(tmp_path, "locks/x.txt", {"torch": "2.13.0", "numba": "0.66.0"})
    r = el.comparar(v, lk)
    assert r["reproduces_lock"] and not r["missing"] and not r["version_mismatch"]


def test_una_version_distinta_rompe_la_reproducibilidad(tmp_path: Path) -> None:
    """★ El caso REAL: `torch` 2.12.0 donde el lock sella 2.13.0."""
    v = _venv_falso(tmp_path, "ante", {"torch": "2.12.0"})
    lk = _lock(tmp_path, "locks/x.txt", {"torch": "2.13.0"})
    r = el.comparar(v, lk)
    assert not r["reproduces_lock"]
    assert r["version_mismatch"] == [{"name": "torch", "locked": "2.13.0", "installed": "2.12.0"}]


def test_un_pin_ausente_rompe_la_reproducibilidad(tmp_path: Path) -> None:
    """`ante_nf` no tenía `transformers` ni `chronos-forecasting`, que el lock sí sella."""
    v = _venv_falso(tmp_path, "ante_nf", {"torch": "2.13.0"})
    lk = _lock(tmp_path, "locks/x.txt", {"torch": "2.13.0", "transformers": "5.13.1"})
    r = el.comparar(v, lk)
    assert not r["reproduces_lock"] and r["missing"] == [{"name": "transformers", "locked": "5.13.1"}]


def test_los_extras_se_reportan_pero_NO_bloquean(tmp_path: Path) -> None:
    """`ante` es también el intérprete de dvc y mlflow: bloquear por sus 127 extras sería ruido,
    y un gate ruidoso acaba desactivado. Se listan enteros y no vetan."""
    v = _venv_falso(tmp_path, "ante", {"torch": "2.13.0", "dvc": "3.0.0", "mlflow": "2.0.0"})
    lk = _lock(tmp_path, "locks/x.txt", {"torch": "2.13.0"})
    r = el.comparar(v, lk)
    assert r["reproduces_lock"] is True
    assert r["extra"] == ["dvc", "mlflow"], "los extras tienen que verse, aunque no bloqueen"


def test_los_nombres_se_normalizan_como_manda_la_PEP_503(tmp_path: Path) -> None:
    """`Chronos_Forecasting` y `chronos-forecasting` son el mismo proyecto: contarlos como dos
    inventaría a la vez un ausente y un extra, y el veredicto sería falso por partida doble."""
    v = _venv_falso(tmp_path, "ante", {"Chronos_Forecasting": "2.3.1"})
    lk = _lock(tmp_path, "locks/x.txt", {"chronos-forecasting": "2.3.1"})
    r = el.comparar(v, lk)
    assert r["reproduces_lock"] and not r["extra"] and not r["missing"]


# ═══════════════════════════════ 2 · fail-closed
def test_un_interprete_ausente_levanta(tmp_path: Path) -> None:
    lk = _lock(tmp_path, "locks/x.txt", {"torch": "2.13.0"})
    with pytest.raises(el.EnvLockError, match="no existe"):
        el.comparar(tmp_path / "no_existe", lk)


def test_un_lock_sin_un_solo_pin_levanta(tmp_path: Path) -> None:
    """Un lock vacío haría que TODO entorno «reproduzca»: es el fail-open perfecto."""
    v = _venv_falso(tmp_path, "ante", {"torch": "2.13.0"})
    vacio = tmp_path / "locks" / "vacio.txt"
    vacio.parent.mkdir(parents=True, exist_ok=True)
    vacio.write_text("# sólo comentarios\n--hash=sha256:abc\n", encoding="utf-8")
    with pytest.raises(el.EnvLockError, match="no declara un solo pin"):
        el.comparar(v, vacio)


def test_un_interprete_que_no_puede_censarse_levanta(tmp_path: Path) -> None:
    """Un intérprete roto no puede leerse como «entorno limpio»."""
    venv = tmp_path / "ante"
    (venv / "bin").mkdir(parents=True)
    py = venv / "bin" / "python"
    py.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    py.chmod(py.stat().st_mode | stat.S_IEXEC)
    with pytest.raises(el.EnvLockError, match="no pudo censar"):
        el.comparar(venv, _lock(tmp_path, "locks/x.txt", {"torch": "2.13.0"}))


def test_exigir_reproducibles_nombra_lo_que_hay_que_arreglar(tmp_path: Path, monkeypatch) -> None:
    _venv_falso(tmp_path, "ante", {"torch": "2.12.0"})
    _lock(tmp_path, "locks/x.txt", {"torch": "2.13.0"})
    monkeypatch.setattr(el, "INTERPRETER_LOCKS", (("ante", "locks/x.txt"),))
    with pytest.raises(el.EnvLockError, match="torch"):
        el.exigir_reproducibles(tmp_path)


def test_no_existe_ninguna_puerta_para_saltarse_la_comprobacion() -> None:
    """★ Anti-resurrección de `--skip-consistency-check` (M74-B-R1), por AST y no por texto."""
    import ast

    arbol = ast.parse((RAIZ / "tools" / "check_env_matches_lock.py").read_text(encoding="utf-8"))
    banderas = [
        c.value
        for n in ast.walk(arbol)
        if isinstance(n, ast.Call)
        for c in n.args
        if isinstance(c, ast.Constant) and isinstance(c.value, str) and c.value.startswith("--")
    ]
    assert not [b for b in banderas if "skip" in b or "force" in b or "ignore" in b], banderas
    fuente = (RAIZ / "tools" / "check_env_matches_lock.py").read_text(encoding="utf-8")
    assert "os.environ" not in fuente, "una variable de entorno sería el bypass por la puerta de atrás"


# ═══════════════════════════════ 3 · el cableado: sello y runbook
def test_el_sello_del_preflight_MIDE_el_entorno() -> None:
    """Antes hasheaba el archivo de lock y comprobaba que el directorio existiera. No es lo mismo."""
    from tools import campaign_preflight as pf

    sello = pf.seal()
    assert "environment" in sello, "el sello no mide el entorno"
    for venv, _lk in pf.INTERPRETER_LOCKS:
        v = sello["environment"][venv]
        assert set(v) >= {"reproduces_lock", "missing", "version_mismatch", "extra", "n_pins"}


def test_la_pareja_interprete_lock_tiene_UNA_autoridad() -> None:
    """Estaba escrita en el preflight y habría que escribirla otra vez en el verificador: dos
    copias de la misma pareja son dos copias que divergen."""
    from tools import campaign_preflight as pf

    assert pf.INTERPRETER_LOCKS is el.INTERPRETER_LOCKS


def test_el_runbook_se_niega_a_arrancar_con_el_entorno_divergente() -> None:
    """Y lo hace ANTES de calcular: el daño de esta clase se mide en horas."""
    guion = (RAIZ / "experiments" / "run_rederivation.sh").read_text(encoding="utf-8")
    vivas = [ln for ln in guion.splitlines() if not ln.lstrip().startswith("#")]
    texto = "\n".join(vivas)
    assert "-m tools.check_env_matches_lock" in texto, "el runbook no comprueba el entorno"
    # ★ antes de sellar el manifiesto de la campaña, que es el primer artefacto que escribe
    assert texto.index("tools.check_env_matches_lock") < texto.index("campaign_manifest.json")
    # ⚠️ Sobre las líneas VIVAS, no sobre el archivo entero: el comentario que documenta por qué
    # NO existe un bypass menciona `SKIP_ENV_CHECK`, y buscar en el texto acusaba a la propia
    # documentación. Es la lección de M72 —un inventario por texto delata a quien describe el
    # patrón prohibido— y aquí me delató a mí.
    assert "SKIP_ENV" not in texto and "ALLOW_ENV" not in texto, "apareció un bypass ejecutable"


def test_el_verificador_entra_al_cierre_de_codigo_sellado() -> None:
    """Si el runbook lo invoca, el preflight tiene que sellarlo: un gate fuera del sello puede
    cambiar sin que el sello se entere."""
    from tools import campaign_preflight as pf

    sello = pf.seal()
    assert "tools/check_env_matches_lock.py" in sello["inputs"]["code"]
