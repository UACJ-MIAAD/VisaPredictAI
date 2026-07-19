"""B263/B266/B271: los gates de gobernanza P0R.5 corren en un job DEDICADO, MÍNIMO y SELLADO (`p0r5-governance`) de
.github/workflows/ci.yml, validado ESTRUCTURALMENTE por tools/check_p0r5_governance.py. El contexto del job
(env/defaults/container/if/pasos previos que tocan GITHUB_PATH) o un `run` alterado o `ci-gate` sin la dependencia →
FALLAN. Anti-substring, anti-neutralizador."""

from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile

import tools.check_p0r5_governance as gov

# capturado UNA vez, antes de cualquier monkeypatch de gov.ROOT.
_REAL_CI = open(os.path.join(gov.ROOT, gov._WORKFLOW), encoding="utf-8").read()
_REAL_ROOT = gov.ROOT
_REAL_REGISTRY = json.loads(open(os.path.join(gov.ROOT, gov._ACTION_REGISTRY), encoding="utf-8").read())


def _run_on(text: str, monkeypatch) -> list[str]:
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, ".github", "workflows"))
    os.makedirs(os.path.join(d, "security"))
    with open(os.path.join(d, ".github", "workflows", "ci.yml"), "w", encoding="utf-8") as fh:
        fh.write(text)
    shutil.copy(os.path.join(_REAL_ROOT, gov._ACTION_REGISTRY), os.path.join(d, gov._ACTION_REGISTRY))
    monkeypatch.setattr(gov, "ROOT", d)
    return gov.problems()


def _run(monkeypatch, *, ci: str | None = None, reg=None) -> list[str]:
    """B278: corre gov.problems() con ci.yml y/o registro de Actions personalizados (dict→JSON o texto crudo)."""
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, ".github", "workflows"))
    os.makedirs(os.path.join(d, "security"))
    with open(os.path.join(d, ".github", "workflows", "ci.yml"), "w", encoding="utf-8") as fh:
        fh.write(_REAL_CI if ci is None else ci)
    raw = json.dumps(_REAL_REGISTRY) if reg is None else (reg if isinstance(reg, str) else json.dumps(reg))
    with open(os.path.join(d, gov._ACTION_REGISTRY), "w", encoding="utf-8") as fh:
        fh.write(raw)
    monkeypatch.setattr(gov, "ROOT", d)
    return gov.problems()


def test_governance_job_is_sealed_in_ci():
    assert gov.problems() == [], "el job p0r5-governance debe estar sellado y ci-gate depender de él (B271)"


def test_b271_job_context_neutralizers_fail(monkeypatch):
    # env/defaults/container/if a nivel del job (mapa != claves exactas) deben fallar aunque el `run` sea exacto.
    for label, ins in {
        "env": "    env:\n      PATH: /tmp/fake-bin\n",
        "defaults": "    defaults:\n      run:\n        shell: bash -c 'exit 0' -- {0}\n",
        "container": "    container: attacker/fake-python:latest\n",
        "services": "    services:\n      x:\n        image: y\n",
        "strategy": "    strategy:\n      matrix:\n        x: [1]\n",
        "if": "    if: false\n",
        "continue_on_error": "    continue-on-error: true\n",
    }.items():
        bad = _REAL_CI.replace("  p0r5-governance:\n", f"  p0r5-governance:\n{ins}", 1)
        assert _run_on(bad, monkeypatch), f"contexto de job `{label}` debe fallar (B271)"


def test_b271_altered_run_and_extra_step_fail(monkeypatch):
    for suffix in (" || true", "; exit 0"):
        bad = _REAL_CI.replace(
            "        run: python tools/check_reflection.py", f"        run: python tools/check_reflection.py{suffix}", 1
        )
        assert _run_on(bad, monkeypatch), f"`{suffix}` en un gate debe fallar (B271)"
    # paso extra (incl. uno que escribe GITHUB_PATH) rompe la secuencia exacta
    extra = _REAL_CI.replace(
        "      - name: Commit frontier contract (fingerprint + autoridad)\n",
        "      - run: echo /tmp/fake >> $GITHUB_PATH\n      - name: Commit frontier contract (fingerprint + autoridad)\n",
        1,
    )
    assert _run_on(extra, monkeypatch), "un paso extra (GITHUB_PATH) debe fallar (B271)"


def test_b271_runner_timeout_permissions_exact(monkeypatch):
    for frm, to in (
        ("runs-on: ubuntu-24.04", "runs-on: ubuntu-latest"),
        ("timeout-minutes: 10", "timeout-minutes: 30"),
        ("      contents: read", "      contents: write"),
    ):
        bad = _REAL_CI.replace(frm, to, 1)
        assert _run_on(bad, monkeypatch), f"cambiar `{frm}`→`{to}` debe fallar (B271)"


def _gov_replace(old: str, new: str) -> str:
    # muta SÓLO dentro del bloque del job p0r5-governance (hay muchos checkout/setup-python en otros jobs).
    start = _REAL_CI.index("  p0r5-governance:\n")
    end = _REAL_CI.index("\n  # P0R.3:", start)
    return _REAL_CI[:start] + _REAL_CI[start:end].replace(old, new, 1) + _REAL_CI[end:]


def test_b271_action_pins_and_python_and_fetch_depth(monkeypatch):
    # SHA de acción no registrado / python distinto → fallan (paso != exacto), DENTRO del job de gobernanza.
    bad_sha = _gov_replace("actions/checkout@93cb6efe18208431cddfb8368fd83d5badbf9bfd", "actions/checkout@" + "0" * 40)
    assert _run_on(bad_sha, monkeypatch), "un SHA de checkout no registrado debe fallar (B271)"
    bad_py = _gov_replace("python-version: '3.14'", "python-version: '3.12'")
    assert _run_on(bad_py, monkeypatch), "python-version distinto de 3.14 debe fallar (B271)"


def test_b271_ci_gate_must_depend_on_governance(monkeypatch):
    bad = _REAL_CI.replace(", p0r5-governance]", "]", 1)
    assert any("needs" in p for p in _run_on(bad, monkeypatch)), "ci-gate sin el need debe fallar (B271)"


def test_b271_duplicate_yaml_keys_and_invalid_fail(monkeypatch):
    assert _run_on("jobs:\n  p0r5-governance:\n    name: x\n    name: y\n", monkeypatch), (
        "claves duplicadas deben fallar"
    )
    assert _run_on("jobs: [unbalanced", monkeypatch), "YAML inválido debe fallar cerrado (B271)"


def test_b271_missing_job_fails(monkeypatch):
    bad = _REAL_CI.replace("  p0r5-governance:\n", "  other-job:\n", 1)
    assert _run_on(bad, monkeypatch), "el job renombrado (ausente) debe fallar (B271)"


def test_b271_fail_closed_missing_workflow(monkeypatch):
    monkeypatch.setattr(gov, "ROOT", "/nonexistent_root_p0r5")
    assert gov.problems()


# ---------------------------------------------------------------------------
# B278 — el registro de GitHub Actions se valida ESTRICTAMENTE (reusa check_action_pins.load_registry) y el SHA de
# checkout/setup-python debe igualar la constante de código _BOOTSTRAP_ACTIONS. El gate offline de action-pins corre
# DENTRO del job mínimo. RED_BASE_SHA = 036c8f9: _action_uses hacía json.load permisivo (sin schema/40-hex/node24/
# duplicados) y no comparaba con constante → un registro con defectos de schema, o forjado junto al workflow, se
# ACEPTABA. Las pruebas conductuales usan un ci.yml SIN el paso offline (compatible con 036c8f9) para que el RED sea
# limpio: en 036c8f9 aceptan; aquí `_action_uses` devuelve el problema B278 ANTES del chequeo de pasos.
# ---------------------------------------------------------------------------
_CHECKOUT_SHA = "93cb6efe18208431cddfb8368fd83d5badbf9bfd"
_CI_NO_OFFLINE = _REAL_CI.replace(
    "      - name: GitHub Actions positive registry (offline)\n        run: python tools/check_action_pins.py\n", "", 1
)


def test_b278_valid_control_passes(monkeypatch):
    assert _run(monkeypatch) == [], "el ci.yml + registro reales deben pasar (B278)"


def test_b278_forged_registry_and_workflow_together_fail(monkeypatch):
    evil = "d" * 40  # SHA de 40 hex VÁLIDO pero != la constante revisada
    reg = copy.deepcopy(_REAL_REGISTRY)
    reg["actions"]["actions/checkout"]["sha"] = evil
    ci = _CI_NO_OFFLINE.replace(_CHECKOUT_SHA, evil)  # el workflow "coincide" con el registro forjado
    assert any("B278" in p for p in _run(monkeypatch, ci=ci, reg=reg)), "registro+workflow forjados juntos deben fallar (B278)"  # fmt: skip


def test_b278_permissive_registry_flaws_rejected(monkeypatch):
    # el SHA sigue siendo el REAL → 036c8f9 construye uses válidos y ACEPTA; load_registry (nuevo) rechaza por schema.
    def mut(fn):
        r = copy.deepcopy(_REAL_REGISTRY)
        fn(r)
        return r

    cases = {
        "schema_true": mut(lambda r: r.__setitem__("schema_version", True)),
        "extra_top": mut(lambda r: r.__setitem__("evil", 1)),
        "node20": mut(lambda r: r["actions"]["actions/checkout"].__setitem__("runtime", "node20")),
        "empty_version": mut(lambda r: r["actions"]["actions/checkout"].__setitem__("version", "")),
        "missing_top": mut(lambda r: r.pop("note")),
    }
    for name, reg in cases.items():
        assert any("B278" in p for p in _run(monkeypatch, ci=_CI_NO_OFFLINE, reg=reg)), f"registro inválido {name} debe fallar (B278)"  # fmt: skip
    # clave JSON duplicada (texto crudo): inyecta un segundo "note" al inicio del objeto raíz
    dup = '{"note": "dup",' + json.dumps(_REAL_REGISTRY)[1:]
    assert any("B278" in p for p in _run(monkeypatch, ci=_CI_NO_OFFLINE, reg=dup)), "clave JSON duplicada debe fallar (B278)"  # fmt: skip


def test_b278_bad_sha_forms_rejected(monkeypatch):
    # SHA no-40-hex: rechazado en ambos SHAs (load_registry en HEAD; mismatch de paso en 036c8f9). Regresión de dureza.
    for bad in ("evil-ref", "v6", "d" * 39, "d" * 41):
        reg = copy.deepcopy(_REAL_REGISTRY)
        reg["actions"]["actions/checkout"]["sha"] = bad
        assert _run(monkeypatch, ci=_CI_NO_OFFLINE, reg=reg), f"SHA {bad!r} debe rechazarse (B278)"


def test_b278_offline_actionpins_step_required_and_exact(monkeypatch):
    # el paso offline de action-pins debe estar presente, en orden y con el comando EXACTO dentro del job mínimo.
    assert _run(monkeypatch, ci=_CI_NO_OFFLINE), "quitar el paso offline de action-pins debe fallar (B278/B271)"
    altered = _REAL_CI.replace(
        "run: python tools/check_action_pins.py", "run: python tools/check_action_pins.py || true", 1
    )
    assert _run(monkeypatch, ci=altered), "alterar el comando del paso offline debe fallar (B278/B271)"
