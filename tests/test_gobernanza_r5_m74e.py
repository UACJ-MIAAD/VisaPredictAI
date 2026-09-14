"""M74-E-R5 · las cinco guardas que la auditoría `8656cf44…` dejó abiertas.

R4 cerró lo grueso, pero la auditoría separada dictaminó **NO APTO** por cinco puertas que seguían
abiertas y por un cambio de entorno más ancho que su evidencia:

* **B1** — la regeneración en bloque movió `numpy 2.4.6 → 2.5.3` y decenas de transitivos por una
  dependencia exclusiva del perfil `model`. R5 preserva los pines pre-R4 y añade **sólo** optuna.
* **B2** — `UNGOVERNED_OK` permitía `pip` y `visapredictai` **por su nombre**: aceptaba otro
  `visapredictai==1.0.0` o un editable apuntando a otro checkout.
* **B3** — «imports diferidos resueltos» usaba `find_spec`: comprobaba PRESENCIA, no importabilidad.
* **B4** — el preflight sella `data/snapshots`, pero **un directorio vacío también se sella**.
* **B7** — tres compromisos operativos sin aplicar.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from tools import check_entrypoint_smoke as ce  # noqa: E402
from tools import check_env_matches_lock as el  # noqa: E402

MODELADO = pytest.mark.skipif(importlib.util.find_spec("darts") is None, reason="extra `model` (darts) ausente")


def _pines(ruta: Path) -> dict[str, str]:
    d = {}
    for ln in ruta.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9._-]+)==(\S+)", ln.split("#")[0].strip())
        if m:
            d[m.group(1).lower().replace("_", "-")] = m.group(2)
    return d


# ═══════════════════════════════ B1 · el stack numérico pre-R4, intacto
def test_el_stack_numerico_es_el_de_pre_R4() -> None:
    """★ Añadir una dependencia del perfil `model` no puede refrescar el núcleo numérico."""
    p = _pines(RAIZ / "locks" / "model-cpu.txt")
    assert p["numpy"] == "2.4.6" and p["scikit-learn"] == "1.9.0"
    assert p["numba"] == "0.66.0" and p["llvmlite"] == "0.48.0" and p["joblib"] == "1.5.3"
    assert p["torch"] == "2.13.0" and p["optuna"] == "4.9.0"


def test_los_perfiles_ajenos_no_se_refrescaron() -> None:
    """`runtime`, `dev` y `deep` no tienen por qué moverse por una dependencia de `model`."""
    fin = subprocess.run(
        ["git", "diff", "--name-only", "93bc644", "--", "locks/"], cwd=RAIZ, capture_output=True, text=True, check=True
    )
    tocados = set(fin.stdout.split())
    assert tocados == {"locks/model-cpu.txt", "locks/model-cpu-linux-x86_64.txt", "locks/lockset.json"}, tocados


def test_solo_optuna_y_su_cierre_entraron() -> None:
    """Ni un pin preexistente movido: es la condición exacta que la auditoría impuso."""
    antes = _pines(
        Path(
            subprocess.run(
                ["git", "show", "93bc644:locks/model-cpu.txt"], cwd=RAIZ, capture_output=True, text=True, check=True
            ).stdout
            and "/dev/null"
        )
    )
    crudo = subprocess.run(["git", "show", "93bc644:locks/model-cpu.txt"], cwd=RAIZ,
                           capture_output=True, text=True, check=True).stdout  # fmt: skip
    antes = {}
    for ln in crudo.splitlines():
        m = re.match(r"^([A-Za-z0-9._-]+)==(\S+)", ln.split("#")[0].strip())
        if m:
            antes[m.group(1).lower().replace("_", "-")] = m.group(2)
    ahora = _pines(RAIZ / "locks" / "model-cpu.txt")
    assert sorted(set(ahora) - set(antes)) == ["alembic", "colorlog", "mako", "optuna", "sqlalchemy"]
    assert not set(antes) - set(ahora), "se perdió un pin"
    assert not [k for k in set(antes) & set(ahora) if antes[k] != ahora[k]], "se movió un pin preexistente"


def test_la_excepcion_de_numpy25_sobra_y_se_retiro() -> None:
    """Una excepción que ya no silencia nada es deuda que parece gobernanza."""
    reg = json.loads((RAIZ / "security" / "warnings_registry.json").read_text(encoding="utf-8"))
    ids = {w["id"] for w in reg["warnings"]}
    assert "numpy25-statespace-shape-assignment" not in ids
    assert len(reg["warnings"]) == 8


# ═══════════════════════════════ B2 · las excepciones se ACREDITAN
def test_el_gate_acredita_el_editable_y_el_toolchain() -> None:
    """★ Un permiso por nombre es un permiso a cualquiera que se llame así."""
    fuente = (RAIZ / "tools" / "check_env_matches_lock.py").read_text(encoding="utf-8")
    assert "direct_url" in fuente and "editable" in fuente
    fn = next(
        n for n in ast.walk(ast.parse(fuente)) if isinstance(n, ast.FunctionDef) and n.name == "acreditar_excepciones"
    )
    cuerpo = ast.get_source_segment(fuente, fn) or ""
    for exigencia in ("pip", "vp_model", "dir_info"):
        assert exigencia in cuerpo, f"no se acredita {exigencia}"


@MODELADO
def test_un_editable_que_apunta_a_otro_checkout_no_acredita() -> None:
    """El caso real: el `ante` de R9 tiene el editable apuntando a otro worktree."""
    r9 = Path("/Users/haowei/Documents/Anteproyecto/VisaPredictAI")
    if not (r9 / "ante" / "bin" / "python").exists():
        pytest.skip("el worktree R9 no está disponible")
    problemas = el.acreditar_excepciones(r9 / "ante", RAIZ, {"pip": "26.1.2"}, ["visapredictai"])
    assert any("editable apunta" in p or "vp_model` resuelve" in p for p in problemas), problemas


# ═══════════════════════════════ B3 · se IMPORTA, no se busca
def test_el_smoke_importa_de_verdad_los_diferidos() -> None:
    """`find_spec` da por bueno un paquete presente pero roto por una dependencia binaria."""
    fuente = (RAIZ / "tools" / "check_entrypoint_smoke.py").read_text(encoding="utf-8")
    fn = next(
        n
        for n in ast.walk(ast.parse(fuente))
        if isinstance(n, ast.FunctionDef) and n.name == "deferred_imports_resolve"
    )
    # ⚠️ Sobre las llamadas REALES, no sobre el texto: el comentario que explica por qué NO se usa
    # `find_spec` lo menciona, y una búsqueda literal acusaba a la propia documentación. Cuarta vez
    # en este lote que un inventario por texto delata a quien describe el patrón prohibido.
    llamadas = {n.func.attr for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "find_spec" not in llamadas, "sigue comprobando presencia en vez de importabilidad"
    cuerpo = ast.get_source_segment(fuente, fn) or ""
    assert "no se puede importar" in cuerpo, "sin diagnóstico por paquete"


def test_el_universo_del_smoke_cubre_las_invocaciones_por_modulo() -> None:
    guion = (RAIZ / "experiments" / "run_rederivation.sh").read_text(encoding="utf-8")
    assert "vp_model.run_tuning" in ce.ANTE_MODULE.findall(guion)


# ═══════════════════════════════ B4 · el linaje, fail-closed
def test_el_runbook_exige_el_linaje_tras_construir_el_almacen() -> None:
    """Un directorio de snapshots vacío se sella igual de bien que uno lleno."""
    vivas = "\n".join(
        ln
        for ln in (RAIZ / "experiments" / "run_rederivation.sh").read_text(encoding="utf-8").splitlines()
        if not ln.lstrip().startswith("#")
    )
    assert "tools/check_source_lineage.py" in vivas
    assert vivas.index("pipeline.build_database") < vivas.index("check_source_lineage.py")
    assert "run_req $ANTE tools/check_source_lineage.py" in vivas, "el linaje no puede ser best-effort"


@MODELADO
def test_el_gate_de_linaje_rechaza_un_directorio_vacio(tmp_path: Path) -> None:
    from tools import check_source_lineage as csl

    (tmp_path / "data" / "snapshots").mkdir(parents=True)
    (tmp_path / "data" / "processed").mkdir(parents=True)
    (tmp_path / "data" / "processed" / "visapredict.duckdb").write_bytes(b"x")
    with pytest.raises(csl.LineageError, match="no tiene un solo snapshot"):
        csl.verify(tmp_path)


def test_el_conjunto_elegible_usa_la_misma_autoridad_que_el_cargador() -> None:
    """Derivarlo aparte sería otra lista que diverge."""
    fuente = (RAIZ / "tools" / "check_source_lineage.py").read_text(encoding="utf-8")
    assert "extract_datetime_from_link" in fuente
    assert "extract_datetime_from_link" in (RAIZ / "pipeline" / "db_loaders.py").read_text(encoding="utf-8")


# ═══════════════════════════════ B5/B7 · documentación e higiene operativa
def test_el_freeze_de_cada_entorno_queda_sellado() -> None:
    fuente = (RAIZ / "tools" / "check_env_matches_lock.py").read_text(encoding="utf-8")
    assert "freeze_sha256" in fuente and "extra_versions" in fuente


def test_los_tres_runners_exportan_PYTHONPATH() -> None:
    for sh in ("run_rederivation.sh", "run_campaign.sh", "save_finalists.sh"):
        texto = (RAIZ / "experiments" / sh).read_text(encoding="utf-8")
        vivas = [ln for ln in texto.splitlines() if not ln.lstrip().startswith("#")]
        assert any('export PYTHONPATH="$PWD' in ln for ln in vivas), sh


def test_la_bitacora_es_por_campana() -> None:
    """Redirigir a un nombre fijo hacía que un relanzamiento sobrescribiera la evidencia."""
    texto = (RAIZ / "experiments" / "run_rederivation.sh").read_text(encoding="utf-8")
    vivas = "\n".join(ln for ln in texto.splitlines() if not ln.lstrip().startswith("#"))
    assert "reports/logs/${CAMPAIGN_ID}.log" in vivas
    assert "tee" in vivas, "la campaña no es dueña de su bitácora"


def test_la_cabecera_ya_no_llama_best_effort_al_tuning() -> None:
    """Una documentación que contradice la conducta es peor que ninguna."""
    # ⚠️ Sobre la ENUMERACIÓN, acotada a su frase: la nota que explica la corrección menciona
    # «búsqueda de tuning» al contarla, y buscarla en 300 caracteres arrastraba esa nota.
    texto = (RAIZ / "experiments" / "run_rederivation.sh").read_text(encoding="utf-8")
    cabecera = texto[: texto.index("set -uo pipefail")]
    i = cabecera.index("Quedan best-effort")
    frase = cabecera[i : cabecera.index(".", i)]
    assert "búsqueda de tuning" not in frase, frase
    assert "BÚSQUEDA DE TUNING" in cabecera, "la enumeración no la declara obligatoria"


def test_la_preinscripcion_no_fija_un_cierre_en_prosa() -> None:
    """Afirmaba `77/5/12` y citaba un preflight que dejó de ser el vigente.

    ⚠️ La primera versión de ESTA prueba exigía `85 / 5 / 12` en la prosa, y caducó dentro del mismo
    lote: cablear el gate de linaje llevó el cierre a 86. Fijar el número correcto en un documento
    reproduce el defecto que se corrige. Lo que se exige ahora es la AUSENCIA de un cierre tecleado
    que se presente como vigente, y que §8.6 conste como desviación.
    """
    import re as _re

    p = Path("/Users/haowei/Documents/Anteproyecto/Prompts/MLOPS_V2_EJECUCION_2026-09/lote_M74A/M74A_PREINSCRIPCION.md")
    if not p.is_file():
        pytest.skip("la preinscripción vive fuera del repositorio")
    texto = p.read_text(encoding="utf-8")
    # ⚠️ La FRASE ORIGINAL completa, no un fragmento: la corrección fechada CITA «no cambian el
    # cierre computacional `77/5/12`» para decir qué afirmaba, y el fragmento acusaba a la cita.
    # Quinta vez en este lote que una comprobación por texto delata a quien documenta el patrón.
    assert "Estas decisiones no cambian el cierre computacional" not in texto, "sigue la afirmación original"
    vigentes = _re.findall(r"cierre vigente es\s*`?\d+\s*/\s*\d+\s*/\s*\d+", texto)
    assert not vigentes, f"vuelve a fijar un cierre en prosa: {vigentes}"
    assert "DESVIACIÓN REPORTABLE" in texto
