"""D8: el índice normativo de `docs/` se sostiene solo, o falla cerrado.

`docs/ENGINEERING.md` declara, por documento: qué autoridad tiene, quién lo consume y en qué
clase vive (canónico · generado · histórico). Esas afirmaciones son verificables, así que aquí
se verifican: enlaces rotos, fuentes que no existen, autoridad duplicada entre dos documentos y
backlinks sin reciprocidad hacen fallar la suite. Sin excepciones silenciosas — si el índice se
queda atrás respecto del repo, esta prueba lo dice antes que un lector.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "docs" / "ENGINEERING.md"
ADR_DIR = ROOT / "docs" / "adr"

# Los documentos que D8 consolida bajo el índice; cada uno debe declarar su clase y su autoridad
# y devolver el enlace (reciprocidad).
CONSOLIDATED = (
    "docs/ARCHITECTURE.md",
    "docs/FAILURE_MATRIX.md",
    "docs/STORAGE_POLICY.md",
    "docs/DEBT_BASELINE.md",
    "docs/dead_code_report.md",
    "docs/mlops_experimentos.md",
    "docs/ROADMAP.md",
)
CLASSES = ("canónico", "generado", "histórico")
LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
ROW = re.compile(r"^\|\s*\[?`?([A-Za-z0-9_./-]+\.(?:md|json|svg|yml|py))`?\]?[^|]*\|", re.M)


def _index() -> str:
    assert INDEX.exists(), "D8 exige un índice normativo en docs/ENGINEERING.md"
    return INDEX.read_text(encoding="utf-8")


def _rows(text: str) -> list[list[str]]:
    out = []
    for line in text.splitlines():
        if line.startswith("|") and line.count("|") >= 5 and not set(line) <= set("|- :"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if cells and not cells[0].lower().startswith(("documento", "fuente")):
                out.append(cells)
    return out


# --- enlaces ----------------------------------------------------------------------


def _targets(md: Path) -> list[str]:
    out = []
    for raw in LINK.findall(md.read_text(encoding="utf-8")):
        target = raw.split("#")[0].strip()
        if target and not target.startswith(("http://", "https://", "mailto:")):
            out.append(target)
    return out


@pytest.mark.parametrize("doc", ["docs/ENGINEERING.md", "docs/adr/0003-campaign-transaction.md"])
def test_no_broken_links(doc: str) -> None:
    md = ROOT / doc
    assert md.exists(), doc
    broken = [t for t in _targets(md) if not (md.parent / t).resolve().exists()]
    assert not broken, f"{doc}: enlaces rotos {broken}"


def test_every_source_named_by_the_index_exists() -> None:
    missing = []
    for cells in _rows(_index()):
        m = re.match(r"^\[?`?([A-Za-z0-9_./-]+)`?\]?", cells[0].replace("**", ""))
        if not m:
            continue
        name = m.group(1)
        if "/" not in name and not name.endswith((".md", ".json", ".svg", ".yml", ".py")):
            continue
        candidate = ROOT / name if "/" in name else ROOT / "docs" / name
        if not candidate.exists():
            missing.append(name)
    assert not missing, f"el índice nombra fuentes inexistentes: {missing}"


# --- autoridad ---------------------------------------------------------------------


def _row_name(cell: str) -> str:
    """Nombre de archivo de la primera celda, con límite exacto: `MLOPS_ARCHITECTURE.md`
    CONTIENE `ARCHITECTURE.md`, y una coincidencia por subcadena confunde las dos filas."""
    m = re.search(r"\(([^)]+)\)", cell) or re.search(r"`([^`]+)`", cell)
    return (m.group(1) if m else cell).split("/")[-1].strip()


def test_each_consolidated_document_has_exactly_one_row() -> None:
    text = _index()
    for doc in CONSOLIDATED:
        name = doc.split("/")[-1]
        hits = [c for c in _rows(text) if _row_name(c[0]) == name]
        assert len(hits) == 1, f"{doc}: {len(hits)} filas en el índice (debe haber exactamente una)"


def test_no_two_documents_claim_the_same_authority() -> None:
    """Dos filas con la MISMA autoridad significan que nadie sabe cuál manda."""
    text = _index()
    seen: dict[str, str] = {}
    dupes = []
    for cells in _rows(text):
        if len(cells) < 4:
            continue
        authority = cells[2].strip().lower()
        if not authority or authority in {"—", "-"}:
            continue
        if authority in seen:
            dupes.append((authority, seen[authority], cells[0]))
        seen[authority] = cells[0]
    assert not dupes, f"autoridad duplicada: {dupes}"


def test_every_row_declares_a_class_and_consumers() -> None:
    for cells in _rows(_index()):
        assert len(cells) >= 4, f"fila incompleta: {cells}"
        assert any(k in cells[1].lower() for k in CLASSES), f"{cells[0]}: sin clase declarada"
        assert cells[3].strip() not in ("", "—", "-"), f"{cells[0]}: sin consumidores declarados"


def test_the_index_does_not_restate_canonical_figures() -> None:
    """El índice apunta a las autoridades; no republica sus cifras (regla #0)."""
    import json

    kf = json.loads((ROOT / "reports" / "governance" / "key_facts.json").read_text())
    text = _index()
    leaked = [
        k
        for k, v in kf.items()
        if isinstance(v, int) and v > 999 and (f"{v:,}" in text or str(v) in re.sub(r"`[^`]*`", "", text))
    ]
    assert not leaked, f"el índice duplica cifras canónicas: {leaked}"


# --- reciprocidad ------------------------------------------------------------------


def test_consolidated_documents_link_back_to_the_index() -> None:
    missing = [d for d in CONSOLIDATED if "ENGINEERING.md" not in (ROOT / d).read_text(encoding="utf-8")]
    assert not missing, f"sin backlink al índice: {missing}"


def test_the_index_links_every_document_it_governs() -> None:
    targets = {t.split("/")[-1] for t in _targets(INDEX)}
    missing = [d for d in CONSOLIDATED if d.split("/")[-1] not in targets]
    assert not missing, f"el índice no enlaza: {missing}"


# --- el guardián muerde: violaciones sembradas -------------------------------------


def problems(text: str, root: Path) -> list[str]:
    """Todos los defectos estructurales del índice, en una sola pasada. Es la función que las
    pruebas de arriba comprueban sobre el árbol real y la de abajo sobre copias envenenadas."""
    out: list[str] = []
    docs = root / "docs"
    for raw in LINK.findall(text):
        target = raw.split("#")[0].strip()
        if target and not target.startswith(("http://", "https://", "mailto:")) and not (docs / target).exists():
            out.append(f"enlace roto: {target}")
    seen: dict[str, str] = {}
    for cells in _rows(text):
        if len(cells) < 4:
            out.append(f"fila incompleta: {cells[0]}")
            continue
        name = _row_name(cells[0])
        candidate = (root / name) if "/" in name else (docs / name)
        if not candidate.exists():
            out.append(f"fuente inexistente: {name}")
        if not any(k in cells[1].lower() for k in CLASSES):
            out.append(f"sin clase: {name}")
        if cells[3].strip() in ("", "—", "-"):
            out.append(f"sin consumidores: {name}")
        authority = cells[2].strip().lower()
        if authority and authority not in ("—", "-"):
            if authority in seen:
                out.append(f"autoridad duplicada: {name} y {seen[authority]}")
            seen[authority] = name
    linked = {t.split("/")[-1] for t in LINK.findall(text)}
    for doc in CONSOLIDATED:
        name = doc.split("/")[-1]
        if name not in linked:
            out.append(f"no enlazado: {name}")
        if "ENGINEERING.md" not in (root / doc).read_text(encoding="utf-8"):
            out.append(f"sin backlink: {name}")
    return out


def test_the_real_index_has_no_structural_problems() -> None:
    assert problems(_index(), ROOT) == []


@pytest.mark.parametrize(
    ("mutacion", "esperado"),
    [
        ("enlace roto", "enlace roto"),
        ("fuente ausente", "fuente inexistente"),
        ("autoridad duplicada", "autoridad duplicada"),
        ("clase borrada", "sin clase"),
        ("consumidores borrados", "sin consumidores"),
    ],
)
def test_seeded_violations_are_caught(tmp_path: Path, mutacion: str, esperado: str) -> None:
    text = _index()
    rows = [ln for ln in text.splitlines() if ln.startswith("|") and "ARCHITECTURE.md`](ARCHITECTURE.md)" in ln]
    assert len(rows) == 1
    row = rows[0]
    cells = [c for c in row.strip("|").split("|")]
    if mutacion == "enlace roto":
        poisoned = text.replace("(ARCHITECTURE.md)", "(NO_EXISTE.md)", 1)
    elif mutacion == "fuente ausente":
        # `_row_name` lee el DESTINO del enlace, no la etiqueta: para simular una fuente
        # inexistente hay que mover el destino, no el texto visible.
        poisoned = text.replace("[`docs/ARCHITECTURE.md`](ARCHITECTURE.md)", "`docs/BORRADO.md`", 1)
    elif mutacion == "autoridad duplicada":
        otra = next(ln for ln in text.splitlines() if "STORAGE_POLICY.md`](STORAGE_POLICY.md)" in ln)
        poisoned = text.replace(
            otra,
            "|"
            + "|".join(
                [otra.strip("|").split("|")[0], otra.strip("|").split("|")[1], cells[2], otra.strip("|").split("|")[3]]
            )
            + "|",
            1,
        )
    elif mutacion == "clase borrada":
        poisoned = text.replace(row, "|" + "|".join([cells[0], "  ", cells[2], cells[3]]) + "|", 1)
    else:
        poisoned = text.replace(row, "|" + "|".join([cells[0], cells[1], cells[2], " — "]) + "|", 1)
    assert poisoned != text, "la mutación debe cambiar el índice"
    found = problems(poisoned, ROOT)
    assert any(esperado in p for p in found), f"{mutacion}: no detectada ({found})"


def test_a_missing_backlink_is_caught(tmp_path: Path) -> None:
    """La reciprocidad se comprueba sobre una COPIA: el árbol real no se toca."""
    import shutil

    work = tmp_path / "repo"
    (work / "docs").mkdir(parents=True)
    for doc in CONSOLIDATED:
        shutil.copy(ROOT / doc, work / doc)
    victim = work / "docs" / "ROADMAP.md"
    victim.write_text(victim.read_text(encoding="utf-8").replace("ENGINEERING.md", "otra-cosa"), encoding="utf-8")
    shutil.copytree(ROOT / "docs" / "adr", work / "docs" / "adr")
    for extra in (
        "CONSISTENCY.md",
        "FORECAST_EVAL.md",
        "PROMOTION_POLICY.md",
        "DVC.md",
        "CLEANING.md",
        "THREAT_MODEL.md",
        "data_dictionary.md",
        "er_diagram.md",
        "MLOPS_ARCHITECTURE.md",
        "experiments_inventory.json",
        "coverage_floors.json",
        "model_catalog.json",
        "debt_baseline.json",
    ):
        shutil.copy(ROOT / "docs" / extra, work / "docs" / extra)
    found = problems(_index(), work)
    assert any("sin backlink: ROADMAP.md" in p for p in found), found


# --- ADR 0003 ----------------------------------------------------------------------


def test_adr_0003_documents_the_transaction_that_the_code_implements() -> None:
    adr = (ADR_DIR / "0003-campaign-transaction.md").read_text(encoding="utf-8")
    src = (ROOT / "tools" / "campaign_state.py").read_text(encoding="utf-8")
    for state in ("running", "computed", "failed", "validated", "published"):
        assert state in adr and f'"{state}"' in src, state
    for mech in ("flock", "os.replace", "O_EXCL", "revision"):
        assert mech in adr and mech in src, f"el ADR cita {mech} pero el código debe implementarlo"
    # honestidad: la máquina existe y está probada, pero ningún runner la conduce todavía
    drivers = [
        p
        for p in list((ROOT / "experiments").rglob("*.py")) + list((ROOT / "experiments").rglob("*.sh"))
        if "campaign_state" in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert not drivers, "si un runner ya conduce la transacción, el ADR debe dejar de decir que no"
    assert "ningún runner" in adr or "ningun runner" in adr


def test_adr_0003_is_registered_in_the_index() -> None:
    assert "0003-campaign-transaction.md" in _index()


def test_adr_numbering_is_contiguous() -> None:
    nums = sorted(int(p.name[:4]) for p in ADR_DIR.glob("0*.md"))
    assert nums == list(range(1, len(nums) + 1)), nums


# --- documentos de seguridad: se auditan, no se reescriben por costumbre -------------


def test_threat_model_only_claims_retirements_that_are_real() -> None:
    """B1 retiró gates concretos; el modelo de amenaza los declara retirados. Si alguno
    volviera a existir, el documento estaría mintiendo y esta prueba lo diría."""
    tm = (ROOT / "docs" / "THREAT_MODEL.md").read_text(encoding="utf-8")
    assert "adr/0002-supply-chain-gates-retired.md" in tm
    resurrected = [
        p
        for p in (
            ".github/workflows/scheduled-quality.yml",
            "tools/audit_python_supply_chain.py",
            "tools/check_supply_chain_triage.py",
            "security/python_advisories.json",
            "docs/SECURITY_TRIAGE.md",
        )
        if (ROOT / p).exists()
    ]
    assert not resurrected, f"el modelo de amenaza los da por retirados pero existen: {resurrected}"


def test_security_triage_lives_in_the_repo_that_owns_its_gate() -> None:
    """El triage npm pertenece al repo web (allí vive su gate semanal). El repo de datos no
    debe recuperar una copia: dos triages serían dos autoridades."""
    assert not (ROOT / "docs" / "SECURITY_TRIAGE.md").exists()
    web = ROOT.parent / "VisaPredictAI_web"
    if not web.exists():  # el repo web es opcional en CI
        pytest.skip("repo web ausente")
    triage = web / "docs" / "SECURITY_TRIAGE.md"
    assert triage.exists()
    assert (web / ".github" / "workflows" / "scheduled-quality.yml").exists(), (
        "el triage cita un gate semanal que debe existir en su propio repo"
    )
