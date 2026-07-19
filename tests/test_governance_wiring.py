"""B263/B266: los gates de gobernanza P0R.5 deben estar CABLEADOS como pasos NOMBRADOS y EXACTOS del job `consistency`
en .github/workflows/ci.yml, validados ESTRUCTURALMENTE (YAML), no por substring. `echo`, comentarios, `|| true`,
`if false`, `continue-on-error`, otro job, claves duplicadas o comandos alterados FALLAN."""

from __future__ import annotations

import os
import tempfile

import tools.check_p0r5_governance as gov

# capturado UNA vez, antes de cualquier monkeypatch de gov.ROOT (que apunta a temp dirs en los tests).
_REAL_CI = open(os.path.join(gov.ROOT, gov._WORKFLOW), encoding="utf-8").read()


def _run_on(text: str, monkeypatch) -> list[str]:
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, ".github", "workflows"))
    with open(os.path.join(d, ".github", "workflows", "ci.yml"), "w", encoding="utf-8") as fh:
        fh.write(text)
    monkeypatch.setattr(gov, "ROOT", d)
    return gov.problems()


def _real() -> str:
    return _REAL_CI


def test_governance_gates_are_wired_in_ci():
    assert gov.problems() == [], "todos los gates deben estar cableados exactos en ci.yml (B263/B266)"


def test_b266_echo_neutralized_fails(monkeypatch):
    ci = (
        "jobs:\n  consistency:\n    steps:\n"
        "      - name: Commit frontier contract (fingerprint + autoridad)\n"
        "        run: echo python tools/check_commit_frontier.py\n"
    )
    assert _run_on(ci, monkeypatch), "un `echo <token>` no debe satisfacer el gate (B266)"


def test_b266_or_true_and_semicolon_fail(monkeypatch):
    for suffix in (" || true", "; exit 0", " && echo done"):
        bad = _real().replace("run: python tools/check_reflection.py", f"run: python tools/check_reflection.py{suffix}")
        assert any("EXACTAMENTE" in p for p in _run_on(bad, monkeypatch)), f"`{suffix}` debe fallar (B266)"


def test_b266_continue_on_error_and_if_fail(monkeypatch):
    coe = _real().replace(
        "      - name: Safe opens contract\n        run: python tools/check_safe_opens.py",
        "      - name: Safe opens contract\n        continue-on-error: true\n        run: python tools/check_safe_opens.py",
    )
    assert any("continue-on-error" in p for p in _run_on(coe, monkeypatch)), "continue-on-error en el paso debe fallar"
    iff = _real().replace(
        "      - name: Safe opens contract\n        run: python tools/check_safe_opens.py",
        "      - name: Safe opens contract\n        if: false\n        run: python tools/check_safe_opens.py",
    )
    assert any("`if`" in p for p in _run_on(iff, monkeypatch)), "`if` en el paso debe fallar (B266)"


def test_b266_job_continue_on_error_fails(monkeypatch):
    ci = "jobs:\n  consistency:\n    continue-on-error: true\n    steps: []\n"
    assert any("continue-on-error" in p for p in _run_on(ci, monkeypatch))


def test_b266_job_level_if_fails(monkeypatch):
    # ronda B: un `if` a nivel de job saltaría TODO el job (todos los gates).
    ci = _real().replace("  consistency:\n", "  consistency:\n    if: false\n", 1)
    assert any("nivel de job" in p for p in _run_on(ci, monkeypatch)), "un if de nivel de job debe fallar (B266)"


def test_b266_yaml_anchor_altering_run_fails(monkeypatch):
    # un anchor/merge que cambie el `run` de un paso a `echo` no debe pasar (se valida el run RESUELTO).
    ci = (
        "jobs:\n  consistency:\n    steps:\n"
        "      - &g\n        name: Commit frontier contract (fingerprint + autoridad)\n        run: echo bypass\n"
        "      - <<: *g\n"
    )
    assert _run_on(ci, monkeypatch), "un anchor que neutralice el run debe fallar (B266)"


def test_b266_wrong_job_fails(monkeypatch):
    # los gates en OTRO job (no consistency) no cuentan.
    ci = _real().replace("  consistency:", "  other_job:", 1)
    assert _run_on(ci, monkeypatch), "gates fuera del job consistency deben fallar (B266)"


def test_b266_missing_name_and_duplicate_step_fail(monkeypatch):
    # paso sin el name exacto
    noname = _real().replace("      - name: B233 historical diagnostic contract\n", "      - ")
    assert _run_on(noname, monkeypatch)
    # dos pasos con el mismo name crítico
    dup = _real().replace(
        "      - name: B233 historical diagnostic contract\n        run: python -m tools.validate_b233_receipt",
        "      - name: B233 historical diagnostic contract\n        run: python -m tools.validate_b233_receipt\n"
        "      - name: B233 historical diagnostic contract\n        run: echo x",
    )
    assert any("aparece 2 veces" in p for p in _run_on(dup, monkeypatch))


def test_b266_duplicate_yaml_keys_and_invalid_yaml_fail(monkeypatch):
    assert _run_on("jobs:\n  consistency:\n    steps: []\n    steps: []\n", monkeypatch), (
        "claves duplicadas deben fallar"
    )
    assert _run_on("jobs: [unbalanced", monkeypatch), "YAML inválido debe fallar cerrado (B266)"


def test_b266_run_not_string_and_steps_not_list_fail(monkeypatch):
    ci = (
        "jobs:\n  consistency:\n    steps:\n"
        "      - name: Commit frontier contract (fingerprint + autoridad)\n        run: [a, b]\n"
    )
    assert _run_on(ci, monkeypatch)
    assert _run_on("jobs:\n  consistency:\n    steps: notalist\n", monkeypatch)


def test_b266_fail_closed_missing_workflow(monkeypatch):
    monkeypatch.setattr(gov, "ROOT", "/nonexistent_root_p0r5")
    assert gov.problems()
