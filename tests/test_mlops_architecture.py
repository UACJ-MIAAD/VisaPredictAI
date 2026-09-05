"""D9: la arquitectura MLOps como documento verificable, no como dibujo bonito.

`experiments/make_mlops_architecture.py` genera DOS artefactos desde fuentes canónicas:
`docs/mlops_architecture.svg` (diagrama de una vista) y `docs/MLOPS_ARCHITECTURE.md` (la
página documental). Lo que este archivo fija:

- ninguna cifra ni estado se teclea: todo sale de `key_facts.json`, de los artefactos de
  gobernanza (`ingestion_state`, `promotion_decision`, `champion_manifest`, `drift_report`),
  del manifiesto de release y de `dvc.yaml`;
- lo commiteado es exactamente lo que el generador produce hoy (si la plataforma cambia y
  nadie regenera, la prueba lo dice);
- los nueve bloques que el encargo exige están documentados;
- el guardián de consistencia vigila ambos artefactos, y una cifra vieja sembrada lo tumba.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
GEN = ROOT / "experiments" / "make_mlops_architecture.py"
SVG = ROOT / "docs" / "mlops_architecture.svg"
MD = ROOT / "docs" / "MLOPS_ARCHITECTURE.md"
RULES = ROOT / "tools" / "consistency_rules.yml"


def _load_gen():
    spec = importlib.util.spec_from_file_location("make_mlops_architecture", GEN)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gen():
    assert GEN.exists(), "el generador de la arquitectura debe existir (D9)"
    return _load_gen()


@pytest.fixture(scope="module")
def facts(gen):
    return gen.collect(ROOT)


# --- 1. lo commiteado es lo que el generador produce -------------------------------


def test_committed_artifacts_match_the_generator(gen, facts) -> None:
    assert SVG.exists() and MD.exists(), "el SVG canónico y la página deben estar commiteados"
    assert gen.render_svg(facts) == SVG.read_text(encoding="utf-8")
    assert gen.render_md(facts) == MD.read_text(encoding="utf-8")


def test_generation_is_deterministic(gen, facts, tmp_path: Path) -> None:
    """Sin timestamps ni orden de diccionario suelto: dos corridas dan los mismos bytes."""
    a = (gen.render_svg(facts), gen.render_md(facts))
    b = (gen.render_svg(gen.collect(ROOT)), gen.render_md(gen.collect(ROOT)))
    assert a == b


def test_generator_writes_only_the_two_documents(gen, tmp_path: Path) -> None:
    """Regenerar no puede tocar el release vivo ni ningún canónico."""
    watched = [
        "reports/release/release_manifest.json",
        "reports/governance/MODEL_CARD.md",
        "reports/governance/key_facts.json",
        "reports/governance/promotion_decision.json",
        "dvc.lock",
    ]
    before = {p: (ROOT / p).read_bytes() for p in watched}
    gen.main()
    after = {p: (ROOT / p).read_bytes() for p in watched}
    assert before == after
    assert gen.OUTPUTS == (SVG, MD)


# --- 2. cifras y estados: derivados, nunca tecleados -------------------------------


def _text_numbers(svg: str) -> set[str]:
    """Números que el lector VE (solo dentro de <text>/<tspan>), no las coordenadas."""
    out: set[str] = set()
    for chunk in re.findall(r"<(?:text|tspan)\b[^>]*>(.*?)</(?:text|tspan)>", svg, re.S):
        out |= set(re.findall(r"\d[\d,.]*", re.sub(r"<[^>]+>", "", chunk)))
    return {n.rstrip(".,") for n in out}


def _md_numbers(md: str) -> set[str]:
    body = re.sub(r"`[^`]*`", " ", md)  # las rutas y comandos en `código` no son cifras
    return {n.rstrip(".,") for n in re.findall(r"\d[\d,.]*", body)}


def test_every_visible_number_is_derived(gen, facts) -> None:
    allowed = gen.allowed_numbers(facts)
    for label, numbers in (
        ("SVG", _text_numbers(SVG.read_text(encoding="utf-8"))),
        ("MD", _md_numbers(MD.read_text(encoding="utf-8"))),
    ):
        stray = sorted(n for n in numbers if n not in allowed)
        assert not stray, f"{label}: cifras sin fuente canónica {stray}"


def test_structural_constants_are_few_and_cannot_mask_a_fact(gen, facts) -> None:
    """Los pocos números 'de forma' (niveles de banda, horizonte) no pueden coincidir con
    un hecho canónico: si coincidieran, un valor viejo pasaría inadvertido."""
    assert len(gen.STRUCTURAL_NUMBERS) <= 6
    canonical = {str(v) for v in facts["_numeric"].values()}
    assert not (set(gen.STRUCTURAL_NUMBERS) & canonical)


def test_states_match_the_governance_artifacts(facts) -> None:
    g = ROOT / "reports" / "governance"
    ing = json.loads((g / "ingestion_state.json").read_text())
    pro = json.loads((g / "promotion_decision.json").read_text())
    champ = json.loads((g / "champion_manifest.json").read_text())
    drift = json.loads((g / "drift_report.json").read_text())
    rel = json.loads((ROOT / "reports" / "release" / "release_manifest.json").read_text())
    assert facts["release_id"] == rel["release_id"] and facts["n_artifacts"] == rel["n_artifacts"]
    assert facts["panel_vintage"] == ing["panel_vintage"] == rel["panel_vintage"]
    assert facts["n_pairs_live"] == pro["n_pairs_live"]
    assert facts["promotion_decisions"] == {t: v["decision"] for t, v in pro["by_table"].items()}
    assert facts["policy_version"] == pro["policy"]["policy_version"]
    assert facts["champions"] == {t: v["models"] for t, v in champ.items()}
    assert facts["drift_detected"] is drift["drift_detected"]


def test_dag_matches_dvc_yaml(facts) -> None:
    stages = list(yaml.safe_load((ROOT / "dvc.yaml").read_text())["stages"])
    assert facts["dag_stages"] == stages
    svg = SVG.read_text(encoding="utf-8")
    for stage in stages:
        assert stage in svg, f"el DAG dibujado omite el stage {stage}"


def test_key_facts_figures_are_taken_verbatim(facts) -> None:
    kf = json.loads((ROOT / "reports" / "governance" / "key_facts.json").read_text())
    for key in ("n_obs", "n_months", "n_obs_F", "n_series_structural", "n_series_evaluable", "n_models"):
        assert facts[key] == kf[key], key


# --- 3. la página cubre los nueve bloques del encargo ------------------------------


def test_page_documents_the_nine_required_blocks() -> None:
    md = MD.read_text(encoding="utf-8").lower()
    for block in (
        "dag",
        "cron",
        "champion",
        "sombra",
        "gate",
        "drift",
        "guardián",
        "key_facts",
        "manifiesto",
        "provenance",
        "ingesta",
    ):
        assert block.lower() in md, f"la página no documenta: {block}"


def test_page_is_a_single_view_with_the_diagram_first() -> None:
    md = MD.read_text(encoding="utf-8")
    assert md.count("\n# ") == 0 and md.startswith("# "), "una sola página, un solo H1"
    assert "mlops_architecture.svg" in md.split("##")[0], "el diagrama abre la página"
    assert len(md.splitlines()) < 200, "una vista, no un tratado"


def test_every_figure_in_the_page_names_its_canonical_source() -> None:
    md = MD.read_text(encoding="utf-8")
    table = [ln for ln in md.splitlines() if ln.startswith("|") and "`" in ln]
    assert table, "debe haber una tabla dato → fuente"
    for source in (
        "reports/governance/key_facts.json",
        "reports/release/release_manifest.json",
        "reports/governance/ingestion_state.json",
        "reports/governance/promotion_decision.json",
        "reports/governance/champion_manifest.json",
        "reports/governance/drift_report.json",
        "dvc.yaml",
    ):
        assert source in md, f"falta la fuente canónica {source}"


def test_svg_is_well_formed_and_self_contained() -> None:
    svg = SVG.read_text(encoding="utf-8")
    ET.fromstring(svg)  # noqa: S314 — artefacto propio del repo, no entrada externa
    naked = svg.replace("http://www.w3.org/2000/svg", "")  # el xmlns no es un recurso externo
    assert "<image" not in naked and "http://" not in naked and "https://" not in naked
    assert len(svg) < 60_000, "un diagrama, no un mapa de bits vectorizado"


# --- 4. el guardián lo vigila ------------------------------------------------------


def test_guardian_watches_both_artifacts() -> None:
    rules = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    groups = rules["artifacts"]
    assert "docs/mlops_architecture.svg" in groups["diagrams"]
    assert "docs/MLOPS_ARCHITECTURE.md" in groups.get("architecture", [])
    covered = [r for r in rules["numeric"] + rules.get("decimal", []) if "architecture" in r["in"]]
    assert len(covered) >= 3, "la página necesita reglas numéricas propias"


@pytest.mark.parametrize("target", ["svg", "md"])
def test_a_stale_figure_is_caught_by_the_guardian(tmp_path: Path, target: str) -> None:
    """RED sembrado: se cambia una cifra viva por la de una añada anterior y el guardián
    debe fallar. Se hace sobre una COPIA del repo: el árbol real no se toca."""
    work = tmp_path / "repo"
    shutil.copytree(
        ROOT,
        work,
        symlinks=True,
        ignore=shutil.ignore_patterns(
            ".git",
            "ante",
            "ante_nf",
            ".vp_envs",
            "data",
            "models",
            "mlartifacts",
            "mlruns_staging",
            "node_modules",
            ".dvc",
            "__pycache__",
            ".mypy_cache",
            ".ruff_cache",
            ".pytest_cache",
        ),
    )
    kf = json.loads((work / "reports" / "governance" / "key_facts.json").read_text())
    live, stale = f"{kf['n_obs']:,}", "27,611"  # la añada anterior a la ingesta manual A7
    path = (work / "docs" / "mlops_architecture.svg") if target == "svg" else (work / "docs" / "MLOPS_ARCHITECTURE.md")
    text = path.read_text(encoding="utf-8")
    assert live in text, "la cifra viva debe estar en el artefacto para poder envenenarla"
    path.write_text(text.replace(live, stale), encoding="utf-8")
    out = subprocess.run([sys.executable, "tools/check_consistency.py"], cwd=work, capture_output=True, text=True)
    assert out.returncode != 0, "el guardián debe rechazar una cifra de la añada anterior"
    assert stale in (out.stdout + out.stderr)


def test_guardian_passes_on_the_real_tree() -> None:
    out = subprocess.run([sys.executable, "tools/check_consistency.py"], cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "MLOPS_ARCHITECTURE.md" in out.stdout or "artefactos alineados" in out.stdout
