"""B0: el cron publica en `main` con el token de una GitHub App limitada al repositorio.

Guardián focal de `.github/workflows/freeze_and_rebuild.yml` (y del comentario de `ci.yml`).
Cada test bloquea una regresión concreta del diseño B0:

- la action que emite el token está fijada al SHA exacto de su release (no a un tag móvil);
- se autentica con `client-id` (el `app-id` está deprecado en v3) y la clave privada del secret;
- el token pide solo `permission-contents: write` y NO se amplía con `owner`/`repositories`;
- `actions/checkout` recibe ESE token y lo persiste, para que los cuatro `git push` usen la
  identidad de la App (actor de bypass del ruleset) y no `GITHUB_TOKEN`;
- el `GITHUB_TOKEN` del job queda en mínimo privilegio (sin `contents: write` ni `actions: write`);
- `ciwait` observa el run automático `on: push` con `github.token` y ya no despacha ci.yml a mano;
- el job falla cerrado antes de que caduque el token de instalación (1 h);
- antes de cada push corre el gate de autoría única del rango saliente.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CRON = ROOT / ".github" / "workflows" / "freeze_and_rebuild.yml"
CI = ROOT / ".github" / "workflows" / "ci.yml"

APP_TOKEN_ACTION = "actions/create-github-app-token"
APP_TOKEN_SHA = "bcd2ba49218906704ab6c1aa796996da409d3eb1"  # v3.2.0
APP_TOKEN_STEP_ID = "cron-app-token"
CLIENT_ID_EXPR = "${{ vars.VISA_CRON_APP_CLIENT_ID }}"
PRIVATE_KEY_EXPR = "${{ secrets.VISA_CRON_APP_PRIVATE_KEY }}"
TOKEN_EXPR = "${{ steps.cron-app-token.outputs.token }}"
GITHUB_TOKEN_EXPR = "${{ github.token }}"
AUTHORSHIP_GATE = "check_outgoing_authorship.sh"


def _cron() -> dict:
    return yaml.safe_load(CRON.read_text(encoding="utf-8"))


def _update_job() -> dict:
    return _cron()["jobs"]["update"]


def _steps() -> list[dict]:
    return _update_job()["steps"]


def _step_by_uses(prefix: str) -> dict:
    hits = [s for s in _steps() if str(s.get("uses", "")).startswith(prefix)]
    assert len(hits) == 1, f"esperaba exactamente un paso {prefix}@…, hay {len(hits)}"
    return hits[0]


def _step_by_id(step_id: str) -> dict:
    hits = [s for s in _steps() if s.get("id") == step_id]
    assert len(hits) == 1, f"esperaba exactamente un paso id={step_id}, hay {len(hits)}"
    return hits[0]


def test_app_token_action_pinned_to_exact_release_sha():
    step = _step_by_uses(APP_TOKEN_ACTION)
    assert step["uses"] == f"{APP_TOKEN_ACTION}@{APP_TOKEN_SHA}", step["uses"]
    assert step.get("id") == APP_TOKEN_STEP_ID


def test_app_token_uses_client_id_and_private_key_secret():
    with_ = _step_by_uses(APP_TOKEN_ACTION)["with"]
    assert with_["client-id"] == CLIENT_ID_EXPR
    assert with_["private-key"] == PRIVATE_KEY_EXPR
    assert "app-id" not in with_, "app-id está deprecado en v3: usar client-id"


def test_app_token_requests_only_contents_write_and_stays_repo_scoped():
    with_ = _step_by_uses(APP_TOKEN_ACTION)["with"]
    assert with_["permission-contents"] == "write"
    extra_perms = {k for k in with_ if k.startswith("permission-") and k != "permission-contents"}
    assert not extra_perms, f"permisos de más en el token: {sorted(extra_perms)}"
    assert "owner" not in with_ and "repositories" not in with_, "el token debe quedar limitado al repositorio actual"


def test_checkout_authenticates_with_the_app_token_and_persists_it():
    steps = _steps()
    idx_token = next(i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith(APP_TOKEN_ACTION))
    idx_checkout = next(i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith("actions/checkout@"))
    assert idx_token < idx_checkout, "el token de la App debe emitirse ANTES del checkout"
    with_ = steps[idx_checkout]["with"]
    assert with_["token"] == TOKEN_EXPR
    assert with_["persist-credentials"] is True
    assert with_["fetch-depth"] == 0


def test_github_token_of_update_job_is_least_privilege():
    perms = _update_job()["permissions"]
    assert perms["contents"] == "read"
    assert perms["actions"] == "read"
    assert perms["issues"] == "write"
    assert perms["id-token"] == "write"
    assert set(perms) == {"contents", "actions", "issues", "id-token"}


def test_update_job_times_out_before_the_installation_token_expires():
    timeout = _update_job()["timeout-minutes"]
    assert isinstance(timeout, int) and 0 < timeout < 60, timeout


def test_ciwait_watches_the_automatic_push_run_with_github_token():
    step = _step_by_id("ciwait")
    assert step["env"]["GH_TOKEN"] == GITHUB_TOKEN_EXPR
    run = step["run"]
    assert "gh workflow run" not in run, "ciwait ya no despacha ci.yml: los pushes de la App disparan on: push"
    assert "--event push" in run
    assert "gh run watch" in run and "--exit-status" in run, "ciwait debe seguir siendo fail-closed"


def test_no_manual_ci_dispatch_remains_anywhere_in_the_cron():
    assert "gh workflow run ci.yml" not in CRON.read_text(encoding="utf-8")


# Afirmaciones (en comentarios o prosa del workflow) que contradicen el diseño B0: con el
# token de la App, los pushes del cron SÍ disparan `on: push`. Se bloquea la frase exacta y sus
# variantes equivalentes (con/sin acento, singular/plural, "el push"/"los pushes", GITHUB_TOKEN).
OBSOLETE_NO_CI_CLAIM = re.compile(
    r"(push(?:es)?\s+(?:del\s+cron|con\s+GITHUB_TOKEN)[^\n]{0,40}?\bno\s+dispara(?:n)?\b"
    r"|\bNO\s+dispara(?:n)?\s+(?:CI|`?on:\s*push`?)"
    r"|\bno\s+dispara(?:n)?\s+(?:CI\b|`?on:\s*push`?))",
    re.IGNORECASE,
)


def test_no_obsolete_claim_that_cron_pushes_do_not_trigger_ci():
    text = CRON.read_text(encoding="utf-8")
    hits = [m.group(0) for m in OBSOLETE_NO_CI_CLAIM.finditer(text)]
    assert not hits, f"afirmación obsoleta (los pushes de la App SÍ disparan CI): {hits}"


def test_authorship_gate_runs_before_every_push():
    text = CRON.read_text(encoding="utf-8")
    pushes = [m.start() for m in re.finditer(r"^\s*git push\s*$", text, re.M)]
    assert len(pushes) == 4, f"el cron debe tener exactamente 4 pushes, hay {len(pushes)}"
    for pos in pushes:
        window_start = text.rfind("git commit", 0, pos)
        assert window_start != -1
        window = text[window_start:pos]
        assert AUTHORSHIP_GATE in window and "origin/main..HEAD" in window, (
            "cada push debe ir precedido, dentro del mismo bloque, por el gate de autoría única del rango saliente"
        )


def test_commit_identity_is_configured_once_before_any_commit_path():
    """D2-B: UNA sola configuración de identidad, tras el checkout y antes de todo commit.

    Antes vivían cuatro parejas repetidas (una por ruta de commit) y la ruta que las saltaba
    moría con "empty ident". El gate de autoría de cada push NO se toca: sigue habiendo cuatro.
    """
    text = CRON.read_text(encoding="utf-8")
    assert text.count('git config --local user.email "168046724+jrebull@users.noreply.github.com"') == 1
    assert text.count('git config --local user.name "Javier Rebull"') == 1

    steps = _cron()["jobs"]["update"]["steps"]
    names = [s.get("name", "") for s in steps]
    checkout = next(i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith("actions/checkout"))
    identity = next(i for i, s in enumerate(steps) if "user.email" in str(s.get("run", "")))
    assert identity == checkout + 1, f"la identidad debe fijarse justo tras el checkout; pasos: {names}"

    first_commit = next(i for i, s in enumerate(steps) if "git commit" in str(s.get("run", "")))
    assert identity < first_commit, "la identidad se fija antes de cualquier ruta de commit"
    assert re.search(r"^\s*git push\s*$", text, re.M), "los pushes siguen existiendo"


def test_ci_workflow_documents_app_pushes_and_keeps_dispatch_and_concurrency():
    text = CI.read_text(encoding="utf-8")
    ci = yaml.safe_load(text)
    on = ci.get("on") or ci.get(True)  # PyYAML parsea la clave `on` como True
    assert "workflow_dispatch" in on
    assert ci["concurrency"]["cancel-in-progress"] is True
    assert "pushea con GITHUB_TOKEN" not in text, "comentario obsoleto: el cron ya pushea con el token de la App"
    assert "Cron Publisher" in text
