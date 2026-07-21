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
# defensivo para la verificación RED en 2ce76d8 (era sin registro de Actions): {} allí, el registro real aquí.
_REAL_REGISTRY = (
    json.loads(open(os.path.join(gov.ROOT, gov._ACTION_REGISTRY), encoding="utf-8").read())
    if hasattr(gov, "_ACTION_REGISTRY")
    else {}
)


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
            "        run: $GOV_ENV/bin/python tools/check_reflection.py",
            f"        run: $GOV_ENV/bin/python tools/check_reflection.py{suffix}",
            1,
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
    bad_sha = _gov_replace("actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09", "actions/checkout@" + "0" * 40)
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
_CHECKOUT_SHA = "fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09"
_CI_NO_OFFLINE = _REAL_CI.replace(
    "      - name: GitHub Actions positive registry (offline)\n        run: $GOV_ENV/bin/python tools/check_action_pins.py\n",
    "",
    1,
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
        # RC-4: un tag MAYOR/MENOR móvil (`v5`/`v5.1`) ya NO se acepta — sólo release EXACTA `vN.N.N` (un tag mayor puede
        # reapuntar upstream, como v5 → v5.1.0, y disparar drift online).
        "major_tag_version": mut(lambda r: r["actions"]["actions/checkout"].__setitem__("version", "v5")),
        "minor_tag_version": mut(lambda r: r["actions"]["actions/checkout"].__setitem__("version", "v5.1")),
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
        "run: $GOV_ENV/bin/python tools/check_action_pins.py",
        "run: $GOV_ENV/bin/python tools/check_action_pins.py || true",
        1,
    )
    assert _run(monkeypatch, ci=altered), "alterar el comando del paso offline debe fallar (B278/B271)"


# ---------------------------------------------------------------------------
# B275 — `ci-gate` se valida por FORMA EXACTA, no por substring sobre yaml.dump. RED_BASE_SHA = 036c8f9: el check viejo
# sólo exigía `p0r5-governance in needs` y el substring `needs.*.result` en `yaml.dump(gate)` → un decoy `env: {DECOY:
# needs.*.result}` con `if: ${{ false }}` en el paso que debe fallar, o retirar un job requerido distinto de
# p0r5-governance, se ACEPTABA. Las pruebas conductuales usan _CI_NO_OFFLINE (compatible con 036c8f9) para aislar
# ci-gate: en 036c8f9 aceptan; aquí caen por forma exacta.
# ---------------------------------------------------------------------------
_STEP0_IF = "${{ contains(needs.*.result, 'failure') || contains(needs.*.result, 'cancelled') || contains(needs.*.result, 'skipped') }}"


def _ci_gate_replace(old: str, new: str) -> str:
    # muta SÓLO dentro del bloque ci-gate (es el último job → replace sobre el sufijo es seguro).
    i = _CI_NO_OFFLINE.index("  ci-gate:\n")
    return _CI_NO_OFFLINE[:i] + _CI_NO_OFFLINE[i:].replace(old, new, 1)


def test_b275_decoy_env_with_neutralized_step_rejected(monkeypatch):
    ci = _ci_gate_replace("    name: ci-gate\n", "    name: ci-gate\n    env:\n      DECOY: needs.*.result\n")
    ci = ci.replace(_STEP0_IF, "${{ false }}", 1)  # neutraliza el paso que debe fallar
    assert any("B275" in p for p in _run(monkeypatch, ci=ci)), "decoy env + if:false debe fallar (B275)"


def test_b275_job_if_always_and_false_rejected(monkeypatch):
    ci = _ci_gate_replace("    if: always()\n", "    if: ${{ always() && false }}\n")
    assert any("B275" in p for p in _run(monkeypatch, ci=ci)), "if de job != 'always()' exacto debe fallar (B275)"


def test_b275_required_need_removal_rejected(monkeypatch):
    # B292: quitar un need = quitar un job del conjunto DERIVADO → cae.
    for need in ("lint-and-test", "model-tests", "consistency", "supply-chain", "campaign-bundle-contract"):
        ci = _ci_gate_replace(f", {need}", "")
        assert any("B292" in p for p in _run(monkeypatch, ci=ci)), f"quitar el need {need!r} debe fallar (B292)"


def test_b275_unknown_and_duplicate_need_rejected(monkeypatch):
    add = _ci_gate_replace("p0r5-governance]", "p0r5-governance, evil-job]")
    assert any("B292" in p for p in _run(monkeypatch, ci=add)), "un need desconocido (no-job) debe fallar (B292)"
    dup = _ci_gate_replace("p0r5-governance]", "p0r5-governance, consistency]")
    assert any("B275" in p for p in _run(monkeypatch, ci=dup)), "un need duplicado debe fallar (B275)"


# ---------------------------------------------------------------------------
# B292 — `ci-gate.needs` se DERIVA de todos los jobs no-gate (no una constante). RED_BASE_SHA = b781d68: un job nuevo
# no añadido a `needs` NI a la constante `_CI_GATE_NEEDS` se aceptaba en silencio. Las pruebas usan _CI_NO_OFFLINE para
# aislar el chequeo de needs (b781d68 acepta; aquí falla nombrando el job omitido).
# ---------------------------------------------------------------------------
_NEW_JOB = "  new-security-contract:\n    name: new-security-contract\n    runs-on: ubuntu-24.04\n    steps:\n      - run: echo hi\n"  # fmt: skip


def test_b292_new_job_omitted_from_needs_rejected(monkeypatch):
    ci = _CI_NO_OFFLINE.replace("  ci-gate:\n", _NEW_JOB + "  ci-gate:\n", 1)
    probs = _run(monkeypatch, ci=ci)
    assert any("B292" in p and "new-security-contract" in p for p in probs), "un job nuevo omitido de needs debe fallar nombrándolo (B292)"  # fmt: skip


def test_b292_new_job_included_no_needs_problem(monkeypatch):
    ci = _CI_NO_OFFLINE.replace("  ci-gate:\n", _NEW_JOB + "  ci-gate:\n", 1)
    ci = ci.replace("p0r5-governance]", "p0r5-governance, new-security-contract]", 1)
    assert not any("B292" in p for p in _run(monkeypatch, ci=ci)), (
        "un job nuevo incluido en needs no debe disparar B292"
    )


def test_b292_all_current_jobs_required_control(monkeypatch):
    assert _run(monkeypatch) == [], "el ci.yml real (todos los jobs no-gate en needs) debe pasar (B292)"


# ---------------------------------------------------------------------------
# B295 — el escape `_EXPLICITLY_OPTIONAL_JOBS` permitía excluir un job de ci-gate.needs sin registro/razón/expiración.
# RED_BASE_SHA = 0d50cb4: añadir el job a la allowlist lo excusaba. Se ELIMINA el mecanismo; `required = set(all_jobs) -
# {ci-gate}` sin resta de excepción.
# ---------------------------------------------------------------------------
def test_b295_optional_job_symbol_removed():
    assert not hasattr(gov, "_EXPLICITLY_OPTIONAL_JOBS"), "el símbolo del escape optional-job debe eliminarse (B295)"


def test_b295_allowlist_cannot_excuse_omitted_job(monkeypatch):
    # aunque se INTENTE reintroducir la allowlist por monkeypatch, el código ya no la resta → el job omitido cae.
    ci = _CI_NO_OFFLINE.replace("  ci-gate:\n", _NEW_JOB + "  ci-gate:\n", 1)
    monkeypatch.setattr(gov, "_EXPLICITLY_OPTIONAL_JOBS", frozenset({"new-security-contract"}), raising=False)
    probs = _run(monkeypatch, ci=ci)
    assert any("B292" in p and "new-security-contract" in p for p in probs), "una allowlist no debe poder excusar un job omitido (B295)"  # fmt: skip


def test_b275_predicate_and_exit_tampering_rejected(monkeypatch):
    drop = _ci_gate_replace(" || contains(needs.*.result, 'skipped')", "")
    assert any("B275" in p for p in _run(monkeypatch, ci=drop)), "quitar 'skipped' del predicado debe fallar (B275)"
    exit0 = _ci_gate_replace("          exit 1\n", "          exit 0\n")
    assert any("B283" in p for p in _run(monkeypatch, ci=exit0)), "exit 0 en vez de exit 1 debe fallar (B283)"
    coe = _ci_gate_replace(
        "      - name: All required jobs succeeded\n",
        "      - name: All required jobs succeeded\n        continue-on-error: true\n",
    )
    assert any("B275" in p for p in _run(monkeypatch, ci=coe)), "continue-on-error en el paso debe fallar (B275)"


def test_b275_success_step_run_decoy_and_extra_step_rejected(monkeypatch):
    decoy = _ci_gate_replace('echo "ci-gate OK - todos los jobs en success"', 'echo "needs.*.result decoy"')
    assert any("B275" in p for p in _run(monkeypatch, ci=decoy)), "run alterado del paso de éxito debe fallar (B275)"
    extra = _ci_gate_replace("    steps:\n", "    steps:\n      - run: echo sneaky\n")
    assert any("B275" in p for p in _run(monkeypatch, ci=extra)), "un paso extra debe fallar (B275)"


def test_b275_duplicate_yaml_key_in_ci_gate_rejected(monkeypatch):
    dup = _ci_gate_replace("    name: ci-gate\n", "    name: ci-gate\n    name: ci-gate\n")
    assert _run(monkeypatch, ci=dup), "clave YAML duplicada en ci-gate debe fallar cerrado (B275)"


# ---------------------------------------------------------------------------
# B283 — `ci-gate` fija el PROGRAMA COMPLETO del paso que falla (no sólo la última línea) y sella runner/timeout/
# permissions. RED_BASE_SHA = b781d68: _ci_gate_step_problems sólo miraba la última línea, así que un `exit 0` ANTES
# del `exit 1` neutralizaba el gate y pasaba. Las pruebas insignia llaman a `_ci_gate_step_problems` directamente
# (existe en ambos SHAs, misma firma) para aislar el chequeo del programa de los sellos nuevos de runner/timeout.
# ---------------------------------------------------------------------------
def _ci_gate_steps(run0):
    return [
        {"name": gov._CI_GATE_STEP0_NAME, "if": gov._CI_GATE_STEP0_IF, "run": run0},
        {"name": gov._CI_GATE_STEP1_NAME, "run": gov._CI_GATE_STEP1_RUN},
    ]


def test_b283_early_exit_zero_before_exit_one_rejected():
    # insignia: `exit 0` ANTES del `exit 1`; la última línea SIGUE siendo `exit 1` (b781d68 la aceptaba).
    steps = _ci_gate_steps("echo attacker\nexit 0\nexit 1\n")
    assert any("B283" in p for p in gov._ci_gate_step_problems(steps)), "un exit 0 antes de exit 1 debe fallar (B283)"


def test_b283_run_program_variants_rejected():
    # subshell y comando extra: la última línea sigue siendo `exit 1` (RED en b781d68); true||exit1 cambia la última.
    for run0 in (
        "(exit 0)\nexit 1\n",  # subshell que sale 0
        'echo "resultados"\necho sneaky\nexit 1\n',  # comando extra
        "true || exit 1\n",  # OR que nunca llega a exit 1
    ):
        assert any("B283" in p for p in gov._ci_gate_step_problems(_ci_gate_steps(run0))), f"programa {run0!r} debe fallar (B283)"  # fmt: skip
    # control: el programa EXACTO no falla por el chequeo de programa
    assert not any("B283" in p for p in gov._ci_gate_step_problems(_ci_gate_steps(gov._CI_GATE_STEP0_RUN)))


def test_b283_runner_timeout_permissions_sealed(monkeypatch):
    # sellos nuevos: runner pineado (no latest), timeout exacto, permisos vacíos (regresiones de HEAD).
    assert any("B283" in p for p in _run(monkeypatch, ci=_ci_gate_replace("    runs-on: ubuntu-24.04\n", "    runs-on: ubuntu-latest\n"))), "runs-on latest debe fallar (B283)"  # fmt: skip
    for tm in ("true", "5.0", "0"):
        bad = _ci_gate_replace("    timeout-minutes: 5\n", f"    timeout-minutes: {tm}\n")
        assert any("B283" in p for p in _run(monkeypatch, ci=bad)), f"timeout {tm} debe fallar (B283)"
    perm = _ci_gate_replace("    permissions: {}\n", "    permissions:\n      contents: write\n")
    assert any("B283" in p for p in _run(monkeypatch, ci=perm)), "permisos no vacíos deben fallar (B283)"


def test_b283_ci_gate_control_passes(monkeypatch):
    assert _run(monkeypatch) == [], "el ci-gate real (24.04/timeout5/permisos vacíos/programa exacto) debe pasar (B283)"


# ---------------------------------------------------------------------------
# B279 — evidencia RED CONDUCTUAL (no AttributeError) para B271. Module-adaptive: en 2ce76d8 el checker valida los
# gates como pasos de `consistency` y NO mira env/defaults/container del JOB → un `env` neutralizador con los comandos
# intactos se ACEPTA (RED). Aquí el job dedicado exige claves exactas y lo RECHAZA. `_run_ci_only` no escribe el
# registro en la era vieja (que no lo usa) para no provocar un AttributeError. Fe de erratas: el RED previo daba
# `AttributeError _ACTION_REGISTRY`, que sólo probaba ausencia de API.
# ---------------------------------------------------------------------------
def _run_ci_only(ci: str, monkeypatch) -> list[str]:
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, ".github", "workflows"))
    with open(os.path.join(d, ".github", "workflows", "ci.yml"), "w", encoding="utf-8") as fh:
        fh.write(ci)
    if hasattr(gov, "_ACTION_REGISTRY"):  # sólo la era nueva usa el registro positivo de Actions
        os.makedirs(os.path.join(d, "security"))
        shutil.copy(os.path.join(_REAL_ROOT, gov._ACTION_REGISTRY), os.path.join(d, gov._ACTION_REGISTRY))
    monkeypatch.setattr(gov, "ROOT", d)
    return gov.problems()


def test_b279_b271_job_context_neutralizer_must_be_rejected(monkeypatch):
    if getattr(gov, "_JOB", None) == "consistency" and hasattr(gov, "REQUIRED_STEPS"):
        # era 2ce76d8: gates INTACTOS como pasos de `consistency` + un `env` de JOB (neutralizador invisible al checker).
        steps = "".join(f"      - name: {n}\n        run: {c}\n" for n, c in gov.REQUIRED_STEPS.items())
        ci = (
            "jobs:\n  consistency:\n    runs-on: ubuntu-latest\n"
            "    env:\n      PATH: /tmp/attacker-bin\n    steps:\n" + steps
        )
    else:
        # era nueva: p0r5-governance con un `env` de JOB → claves de job != exactas.
        ci = _REAL_CI.replace(
            "  p0r5-governance:\n", "  p0r5-governance:\n    env:\n      PATH: /tmp/attacker-bin\n", 1
        )
    assert _run_ci_only(ci, monkeypatch), "una gobernanza neutralizada por `env` de job debe rechazarse (B279/B271)"
