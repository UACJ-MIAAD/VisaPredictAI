"""E5 · Una sola fuente para las cifras de la épica E, y nada tecleado.

Si una cifra del `.tex`, de la tabla, de la figura o de la tarjeta no sale de
``reports/governance/e5_facts.json``, es que alguien la escribió a mano. Aquí se comprueba que
el artefacto se deriva de E1–E4, que la narrativa negativa sobrevive, que el `.tex` no repite
ningún número suelto y que el release del corte actual **no** se regenera.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import check_consistency as cc  # noqa: E402

FACTS = ROOT / "reports" / "governance" / "e5_facts.json"
MACROS = cc._repo_path("reports/latex/cohorts_facts.tex")
TABLA = cc._repo_path("reports/latex/cohortes.tex")
TEX = cc._repo_path("reports/latex/ProyectoI_VisaPredictAI.tex")
FIGURA = cc._repo_path("reports/latex/Figures/e5_router_effect.pdf")
FUENTES = {
    "cohorts": ROOT / "reports" / "eval" / "series_cohorts.json",
    "scan": ROOT / "reports" / "eval" / "cohort_scan.json",
    "campaign": ROOT / "reports" / "eval" / "e3_cohort_campaign.json",
    "router": ROOT / "reports" / "eval" / "e4_router.json",
    "pool": ROOT / "reports" / "eval" / "e4_router_pool.json",
}


def facts() -> dict:
    return json.loads(FACTS.read_text())


# ------------------------------------------------------------------ derivado, no tecleado
def test_cada_cifra_sale_de_E1_a_E4() -> None:
    """Los conteos del artefacto coinciden con sus fuentes, una por una."""
    f = facts()
    coh = json.loads(FUENTES["cohorts"].read_text())
    rout = json.loads(FUENTES["router"].read_text())
    camp = json.loads(FUENTES["campaign"].read_text())
    assert f["NSeriesEvaluable"] == coh["population"]["n_evaluable"]
    assert f["NEstable"] == coh["cohorts"]["estable"]
    assert f["NNoEstable"] == coh["cohorts"]["no_estable"]
    assert f["RuleVersion"] == coh["rule_version"]
    assert f["RouterCeldas"] == len(rout["cells"])
    assert f["RouterMargen"] == rout["gate"]["material_margin"]
    assert f["RouterAlfa"] == rout["gate"]["holm_alpha"]
    assert f["CampanaLanes"] == camp["n_lanes"]


def test_la_procedencia_lleva_el_hash_de_cada_fuente() -> None:
    prov = facts()["provenance"]
    assert set(prov) == set(FUENTES)
    for nombre, ruta in FUENTES.items():
        assert prov[nombre] == hashlib.sha256(ruta.read_bytes()).hexdigest(), nombre


def test_el_artefacto_esta_al_dia() -> None:
    """Maestro dorado: regenerar no cambia un byte del JSON, las macros ni la tabla."""
    import experiments.build_e5_facts as b

    fresco = b.construir()
    assert json.dumps(fresco, indent=2, sort_keys=True, ensure_ascii=False) + "\n" == FACTS.read_text()
    assert b.macros(fresco) == MACROS.read_text()
    assert b.tabla(fresco) == TABLA.read_text()


def test_regenerar_es_determinista_entre_procesos(tmp_path: Path) -> None:
    salidas = []
    for i in range(2):
        d = tmp_path / f"c{i}"
        d.mkdir()
        subprocess.run(
            [
                sys.executable,
                "experiments/build_e5_facts.py",
                "--out-json",
                str(d / "f.json"),
                "--out-macros",
                str(d / "m.tex"),
                "--out-tabla",
                str(d / "t.tex"),
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            env={"PYTHONHASHSEED": str(i), **__import__("os").environ},
        )
        salidas.append(tuple((d / n).read_bytes() for n in ("f.json", "m.tex", "t.tex")))
    assert salidas[0] == salidas[1]


# ------------------------------------------------------------- la narrativa negativa vive
def test_el_resultado_negativo_sobrevive_en_el_artefacto() -> None:
    f = facts()
    assert f["narrative"]["router_gana"] is False
    assert f["RouterGana"] == 0
    assert f["ScanBate"] == 0
    assert f["CampanaBate"] == 0
    assert f["RouterPierde"] >= 1, "DFF/estable pierde materialmente"
    assert f["RouterFadCortosSignificativos"] == 0, "las mejoras largas de FAD no cubren el corto"
    assert f["RouterFadLargosSignificativos"] > 0
    assert f["RouterDffEstablePeorHTres"] < 0 < f["RouterDffEstableMejorHDoce"]
    assert f["RouterDffNoEstableN"] < 6, "la cohorte inestable de DFF no llega al mínimo de pares"


def test_la_prosa_del_tex_usa_macros_y_no_numeros_sueltos() -> None:
    """RED de cifra tecleada: la subsección cita macros, no dígitos."""
    tex = TEX.read_text()
    inicio = tex.index("Cohortes de estabilidad y enrutado por cohorte")
    fin = tex.index("\\begin{table}[H]", inicio)
    cuerpo = tex[inicio:fin]
    assert "\\cohortFact" in cuerpo
    sueltos = [
        n
        for n in re.findall(r"(?<![\w\\{.])\d+(?:[.,]\d+)?(?![\w}])", cuerpo)
        if n not in {"1", "2"}  # ordinales de la prosa ("en cuatro tiempos" va en letra)
    ]
    assert not sueltos, f"cifras tecleadas en la subsección: {sueltos}"


def test_el_tex_carga_las_macros_y_referencia_tabla_y_figura() -> None:
    tex = TEX.read_text()
    assert "\\input{cohorts_facts}" in tex
    assert "\\input{cohortes.tex}" in tex
    assert "e5_router_effect.pdf" in tex
    assert "\\label{tab:cohortes}" in tex and "\\label{fig:cohortes}" in tex
    assert FIGURA.exists() and FIGURA.stat().st_size > 5_000


# ------------------------------------------------------------------ macros y tabla sanas
def test_ninguna_macro_tiene_un_nombre_invalido_en_latex() -> None:
    """Un nombre con dígito o guion bajo produce un `.tex` que no compila (lección M28)."""
    lineas = [ln for ln in MACROS.read_text().splitlines() if ln.startswith("\\newcommand")]
    assert lineas
    patron = re.compile(r"^\\newcommand\{\\[A-Za-z]+\}\{.*\}$")
    assert [ln for ln in lineas if not patron.match(ln)] == []


def test_el_generador_falla_si_una_clave_pierde_su_macro() -> None:
    """RED: una clave nueva con dígito debe deletrearse, no desaparecer en silencio."""
    import experiments.build_e5_facts as b

    with pytest.raises(ValueError, match="deletrea los dígitos"):
        b.macros({**b.construir(), "RouterH3Nuevo": 1})


def test_la_tabla_escapa_lo_que_latex_trata_como_especial() -> None:
    import experiments.build_e5_facts as b

    assert b._tex("no_estable") == "no\\_estable"
    # Solo el CUERPO: la cabecera lleva el nombre del generador, que sí tiene guiones bajos.
    texto = TABLA.read_text()
    cuerpo = texto[texto.index("\\midrule") : texto.index("\\bottomrule")]
    assert "_" not in cuerpo.replace("\\_", ""), "guion bajo sin escapar en el cuerpo de la tabla"


# ------------------------------------------------- release: spec ampliado, corte intacto
def test_el_spec_del_release_incluye_los_artefactos_de_E5() -> None:
    fuente = (ROOT / "experiments" / "build_release_manifest.py").read_text()
    for ruta in ("reports/governance/e5_facts.json", "reports/eval/series_cohorts.json"):
        assert f'("{ruta}", "required")' in fuente, ruta


def test_la_excepcion_PRE_E5_es_nominal_y_cerrada() -> None:
    import tools.check_contracts as cc

    manifiesto = json.loads((ROOT / "reports" / "release" / "release_manifest.json").read_text())
    sha = hashlib.sha256((ROOT / "reports" / "release" / "release_manifest.json").read_bytes()).hexdigest()
    assert cc._e5_facts_problems(manifiesto, sha) == [], "el corte publicado debe estar acreditado"
    # Cualquier OTRO corte que los omita falla, aunque se parezca.
    for campo, valor in (("release_id", "2026-10-otro"), ("panel_vintage", "2026-10"), ("n_artifacts", 999)):
        problemas = cc._e5_facts_problems({**manifiesto, campo: valor}, sha)
        assert problemas and "NO es el corte PRE-E5" in problemas[0], campo
    assert cc._e5_facts_problems(manifiesto, "0" * 64)


def test_el_corte_publicado_no_se_regenera() -> None:
    """El manifiesto y la tarjeta vigentes conservan su identidad exacta."""
    import tools.check_contracts as cc

    card = ROOT / "reports" / "governance" / "MODEL_CARD.md"
    manifiesto = ROOT / "reports" / "release" / "release_manifest.json"
    assert hashlib.sha256(card.read_bytes()).hexdigest() == cc.LEGACY_CARD_SHA256
    assert hashlib.sha256(manifiesto.read_bytes()).hexdigest() == cc.LEGACY_MANIFEST_SHA256


def test_la_tarjeta_del_proximo_corte_ya_trae_el_bloque_de_cohortes() -> None:
    """El GENERADOR se actualiza; la tarjeta publicada no se toca."""
    import experiments.build_model_card as mc

    texto = mc.render("TEST")
    assert "## 5.1 Cohortes de estabilidad" in texto
    f = facts()
    assert str(f["NEstable"]) in texto and str(f["RouterCeldas"]) in texto
    assert "no está desplegado ni promovido" in texto
    assert "## 5.1" not in (ROOT / "reports" / "governance" / "MODEL_CARD.md").read_text()


def test_la_propuesta_sigue_congelada() -> None:
    """E5 no toca `AnteproyectoVisaPredictAI.tex`; el grupo `proposal` sigue sin reglas required."""
    import yaml

    reglas = yaml.safe_load((ROOT / "tools" / "consistency_rules.yml").read_text())
    assert "proposal" in reglas["frozen"]
    for r in reglas.get("required", []):
        assert "proposal" not in r.get("in", []), r


def test_el_contrato_de_F3_no_se_debilito() -> None:
    """Las reglas nuevas usan el checker TAL CUAL: mismos kinds, ninguna excepción nueva."""
    import yaml

    reglas = yaml.safe_load((ROOT / "tools" / "consistency_rules.yml").read_text())
    e5 = [c for c in reglas["tex_json"] if "cohort" in c["tex"]]
    assert len(e5) == 2
    assert {c["kind"] for c in e5} == {"macros", "table"}
    assert all(c["generator"] == "experiments/build_e5_facts.py" for c in e5)
