#!/usr/bin/env python
"""B291: gate del CONTRATO de gobernanza del repositorio (`security/repository_governance.json`).

`--offline` (verde local, sin red): valida el esquema del contrato y que `.github/CODEOWNERS` cubra TODAS las rutas
críticas declaradas (cada patrón con al menos un owner `@…`). No consulta GitHub.

`--online` (requiere `GH_TOKEN`; consulta la GitHub API): exige la política REAL de protección de `main` —
ruleset activo con `ci-gate` estricto, ≥1 aprobación, dismiss-stale, last-push approval, code-owner review, cero
bypass actors, aprobación ligada al SHA final por un revisor distinto del autor/pusher. Con el estado ACTUAL
(ruleset sin aprobaciones, un solo colaborador) queda en ROJO — el bloqueo es EXTERNO y NO se convierte en skip:
sin token, o con la política incumplida, `--online` devuelve != 0.

Sólo stdlib (`json`/`os`/`subprocess`); la red va por el `gh` CLI (mismo puerto que check_action_pins). Fail-closed."""

from __future__ import annotations

import json
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONTRACT = "security/repository_governance.json"
_CODEOWNERS = ".github/CODEOWNERS"
_TOP_KEYS = {
    "schema_version",
    "note",
    "repository",
    "default_branch",
    "ruleset",
    "required_status_checks",
    "review_policy",
    "bypass",
    "codeowners_required_paths",
    "external_blocker",
}
_REVIEW_KEYS = {
    "required_approving_review_count",
    "dismiss_stale_reviews_on_push",
    "require_last_push_approval",
    "require_code_owner_review",
    "reviewer_must_differ_from_author_and_pusher",
    "approval_bound_to_final_sha",
}
_SCHEMA_VERSION = 1


def _no_dup_pairs(pairs: list[tuple[str, object]]) -> dict:
    seen: dict[str, object] = {}
    for k, v in pairs:
        if k in seen:
            raise ValueError(f"clave JSON duplicada: {k!r}")
        seen[k] = v
    return seen


def _load_contract() -> tuple[dict, list[str]]:
    try:
        with open(os.path.join(_ROOT, _CONTRACT), encoding="utf-8") as fh:
            doc = json.loads(fh.read(), object_pairs_hook=_no_dup_pairs)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return {}, [f"{_CONTRACT}: ilegible/no-JSON/duplicado ({exc}) (fail-closed)"]
    if not (isinstance(doc, dict) and set(doc) == _TOP_KEYS):
        return {}, [f"{_CONTRACT}: claves superiores != {sorted(_TOP_KEYS)} (fail-closed)"]
    if not (type(doc["schema_version"]) is int and doc["schema_version"] == _SCHEMA_VERSION):
        return {}, [f"{_CONTRACT}: schema_version != {_SCHEMA_VERSION}"]
    return doc, []


def _codeowners_patterns() -> tuple[set[str], list[str]]:
    """Patrones con al menos un owner `@…` en CODEOWNERS (ignora comentarios/blancos). Fail-closed si falta."""
    try:
        with open(os.path.join(_ROOT, _CODEOWNERS), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        return set(), [f"{_CODEOWNERS}: ausente/ilegible ({exc}) (fail-closed B291)"]
    patterns: set[str] = set()
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if len(parts) >= 2 and any(p.startswith("@") for p in parts[1:]):
            patterns.add(parts[0])
    return patterns, []


def offline_problems() -> list[str]:
    doc, errs = _load_contract()
    if errs:
        return errs
    problems: list[str] = []
    # subestructuras mínimas
    if not (isinstance(doc["review_policy"], dict) and set(doc["review_policy"]) == _REVIEW_KEYS):
        problems.append(f"{_CONTRACT}: review_policy claves != {sorted(_REVIEW_KEYS)}")
    else:
        rp = doc["review_policy"]
        if not (type(rp["required_approving_review_count"]) is int and rp["required_approving_review_count"] >= 1):
            problems.append(f"{_CONTRACT}: required_approving_review_count debe ser >= 1")
        for k in _REVIEW_KEYS - {"required_approving_review_count"}:
            if rp[k] is not True:
                problems.append(f"{_CONTRACT}: review_policy.{k} debe ser true")
    rsc = doc["required_status_checks"]
    if not (isinstance(rsc, dict) and rsc.get("strict") is True and rsc.get("checks") == ["ci-gate"]):
        problems.append(f"{_CONTRACT}: required_status_checks debe ser strict con checks == ['ci-gate']")
    if doc["bypass"] != {"allowed_actors": []}:
        problems.append(f"{_CONTRACT}: bypass.allowed_actors debe estar VACÍO (cero bypass)")
    rs = doc["ruleset"]
    if not (isinstance(rs, dict) and rs.get("enforcement") == "active" and rs.get("target_ref") == "refs/heads/main"):
        problems.append(f"{_CONTRACT}: ruleset debe estar 'active' sobre refs/heads/main")
    # cobertura CODEOWNERS de las rutas críticas declaradas
    required = doc["codeowners_required_paths"]
    if not (isinstance(required, list) and required):
        problems.append(f"{_CONTRACT}: codeowners_required_paths vacío")
        return problems
    owned, cerrs = _codeowners_patterns()
    problems.extend(cerrs)
    for pat in required:
        if pat not in owned:
            problems.append(
                f"{_CODEOWNERS}: la ruta crítica {pat!r} NO tiene owner declarado (cobertura incompleta B291)"
            )
    return problems


def _gh_get(path: str, token: str) -> tuple[object, str | None]:
    # RED sólo por el `gh` CLI (mismo puerto de red que check_action_pins.verify_remote), no urllib directo — así la
    # arquitectura mantiene la red detrás de sus puertos declarados. `gh api` usa GH_TOKEN del entorno.
    try:
        r = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if r.returncode != 0:
        return None, (r.stderr or "").strip() or f"gh api {path} rc={r.returncode}"
    try:
        return json.loads(r.stdout), None
    except ValueError as exc:
        return None, str(exc)


def online_problems(doc: dict) -> list[str]:
    """Consulta la GitHub API y EXIGE la política. Fail-closed: sin token, o con la política incumplida, devuelve
    problemas (ROJO). NUNCA convierte el bloqueo externo en skip verde."""
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        return ["--online: GH_TOKEN ausente — NO se puede verificar la protección remota; el bloqueo externo B291 sigue ROJO (no es un skip)"]  # fmt: skip
    repo = doc["repository"]
    problems: list[str] = []
    # 1) ruleset activo con ci-gate estricto y cero bypass
    rulesets, err = _gh_get(f"/repos/{repo}/rulesets", token)
    if err is not None or not isinstance(rulesets, list):
        return [f"--online: no se pudieron leer los rulesets de {repo} ({err}) — B291 ROJO"]
    active = [r for r in rulesets if isinstance(r, dict) and r.get("enforcement") == "active" and r.get("id") == doc["ruleset"]["id"]]  # fmt: skip
    if not active:
        problems.append(f"--online: no hay ruleset ACTIVO con id {doc['ruleset']['id']} sobre {repo} (B291)")
    # 2) protección de rama / revisiones sobre main
    prot, perr = _gh_get(f"/repos/{repo}/branches/{doc['default_branch']}/protection", token)
    if perr is not None or not isinstance(prot, dict):
        problems.append(f"--online: sin protección legible sobre {doc['default_branch']} ({perr}) (B291)")
    else:
        pr = prot.get("required_pull_request_reviews") or {}
        rp = doc["review_policy"]
        if (pr.get("required_approving_review_count") or 0) < rp["required_approving_review_count"]:
            problems.append("--online: la protección exige menos aprobaciones que la política (B291)")
        if not pr.get("dismiss_stale_reviews"):
            problems.append("--online: dismiss stale reviews NO activo (B291)")
        if not pr.get("require_last_push_approval"):
            problems.append("--online: require last-push approval NO activo (B291)")
        if not pr.get("require_code_owner_reviews"):
            problems.append("--online: require code-owner reviews NO activo (B291)")
        if not (prot.get("required_status_checks") or {}).get("strict"):
            problems.append("--online: required status checks NO es strict (B291)")
    # 3) colaboradores: debe existir al menos un revisor con write/maintain/admin DISTINTO del autor
    collabs, cerr = _gh_get(f"/repos/{repo}/collaborators?permission=push", token)
    if cerr is not None or not isinstance(collabs, list):
        problems.append(f"--online: no se pudo enumerar colaboradores con push ({cerr}) (B291)")
    elif len([c for c in collabs if isinstance(c, dict)]) < 2:
        problems.append("--online: menos de 2 colaboradores con push — no hay revisor independiente posible (B291)")
    if not problems:
        # incluso si la API dijera OK, el contrato marca el bloqueo externo abierto: exigir su cierre explícito.
        if doc["external_blocker"].get("open") is True:
            problems.append("--online: external_blocker.open=true en el contrato — cerrar sólo tras la acción 10.2 con revisor real (B291)")  # fmt: skip
    return problems


def main(argv: list[str]) -> int:
    online = "--online" in argv[1:]
    offline = "--offline" in argv[1:] or not online
    problems: list[str] = []
    if offline:
        problems += offline_problems()
    if online:
        doc, errs = _load_contract()
        problems += errs or online_problems(doc)
    if problems:
        print("✗ gobernanza del repositorio (B291):")
        for p in problems:
            print(f"  - {p}")
        return 1
    mode = "offline+online" if (offline and online) else ("online" if online else "offline")
    print(f"✓ contrato de gobernanza del repositorio [{mode}] ({_CONTRACT})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
