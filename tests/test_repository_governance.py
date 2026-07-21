"""B291 REDs: contrato de gobernanza del repositorio (`tools/check_repository_governance.py`).

Esquema + cobertura CODEOWNERS (offline, verde local) y respuestas GitHub adversariales (online, que DEBE quedar rojo por
el bloqueo externo). El árbol real: offline verde, online rojo sin token. Cada RED monkeypatchea el contrato/CODEOWNERS o
`_gh_get` para violar una regla."""

from __future__ import annotations

import json
import pathlib

import tools.check_repository_governance as rg

_GOOD = json.loads(pathlib.Path("security/repository_governance.json").read_text())


def _contract(monkeypatch, tmp_path, doc=None, codeowners=None):
    root = str(tmp_path)
    (tmp_path / "security").mkdir()
    (tmp_path / ".github").mkdir()
    (tmp_path / "security" / "repository_governance.json").write_text(json.dumps(doc if doc is not None else _GOOD))
    co = (
        codeowners if codeowners is not None else "\n".join(f"{p} @jrebull" for p in _GOOD["codeowners_required_paths"])
    )
    (tmp_path / ".github" / "CODEOWNERS").write_text(co + "\n")
    monkeypatch.setattr(rg, "_ROOT", root)


# --------------------------------------------------------------------------- offline


def test_real_offline_is_green():
    assert rg.offline_problems() == [], rg.offline_problems()


def test_real_online_without_token_is_red(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert rg.main(["prog", "--online"]) == 1  # el bloqueo externo NO es un skip


def test_clean_synthetic_offline_passes(tmp_path, monkeypatch):
    _contract(monkeypatch, tmp_path)
    assert rg.offline_problems() == [], rg.offline_problems()


def test_missing_codeowners_path_fails(tmp_path, monkeypatch):
    # CODEOWNERS omite /locks/ → cobertura incompleta
    co = "\n".join(f"{p} @jrebull" for p in _GOOD["codeowners_required_paths"] if p != "/locks/")
    _contract(monkeypatch, tmp_path, codeowners=co)
    assert any("/locks/" in p and "cobertura incompleta" in p for p in rg.offline_problems()), rg.offline_problems()


def test_codeowners_pattern_without_owner_fails(tmp_path, monkeypatch):
    # /locks/ aparece pero sin @owner → no cuenta como cubierto
    co = "\n".join((f"{p}" if p == "/locks/" else f"{p} @jrebull") for p in _GOOD["codeowners_required_paths"])
    _contract(monkeypatch, tmp_path, codeowners=co)
    assert any("/locks/" in p for p in rg.offline_problems()), rg.offline_problems()


def test_missing_codeowners_file_fails(tmp_path, monkeypatch):
    root = str(tmp_path)
    (tmp_path / "security").mkdir()
    (tmp_path / "security" / "repository_governance.json").write_text(json.dumps(_GOOD))
    monkeypatch.setattr(rg, "_ROOT", root)  # sin .github/CODEOWNERS
    assert any("CODEOWNERS" in p and "ausente" in p for p in rg.offline_problems()), rg.offline_problems()


def test_zero_approval_policy_rejected(tmp_path, monkeypatch):
    bad = json.loads(json.dumps(_GOOD))
    bad["review_policy"]["required_approving_review_count"] = 0
    _contract(monkeypatch, tmp_path, doc=bad)
    assert any(">= 1" in p for p in rg.offline_problems()), rg.offline_problems()


def test_non_empty_bypass_rejected(tmp_path, monkeypatch):
    bad = json.loads(json.dumps(_GOOD))
    bad["bypass"]["allowed_actors"] = ["evil"]
    _contract(monkeypatch, tmp_path, doc=bad)
    assert any("bypass" in p for p in rg.offline_problems()), rg.offline_problems()


def test_non_strict_ci_gate_rejected(tmp_path, monkeypatch):
    bad = json.loads(json.dumps(_GOOD))
    bad["required_status_checks"]["strict"] = False
    _contract(monkeypatch, tmp_path, doc=bad)
    assert any("strict" in p for p in rg.offline_problems()), rg.offline_problems()


def test_review_flag_false_rejected(tmp_path, monkeypatch):
    bad = json.loads(json.dumps(_GOOD))
    bad["review_policy"]["require_code_owner_review"] = False
    _contract(monkeypatch, tmp_path, doc=bad)
    assert any("require_code_owner_review" in p for p in rg.offline_problems()), rg.offline_problems()


def test_duplicate_json_key_fails(tmp_path, monkeypatch):
    root = str(tmp_path)
    (tmp_path / "security").mkdir()
    (tmp_path / ".github").mkdir()
    (tmp_path / "security" / "repository_governance.json").write_text('{"schema_version": 1, "schema_version": 2}')
    (tmp_path / ".github" / "CODEOWNERS").write_text("/x @a\n")
    monkeypatch.setattr(rg, "_ROOT", root)
    assert any("duplicad" in p.lower() for p in rg.offline_problems()), rg.offline_problems()


# --------------------------------------------------------------------------- online (adversarial GitHub responses)


def _mock_gh(responses):
    """responses: dict path-substring -> (obj, err). Devuelve un _gh_get falso."""

    def fake(path, token):
        for key, val in responses.items():
            if key in path:
                return val
        return None, f"unmocked {path}"

    return fake


def test_online_no_active_ruleset_red(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setattr(rg, "_gh_get", _mock_gh({"/rulesets": ([], None), "/protection": ({}, None), "/collaborators": ([{}, {}], None)}))  # fmt: skip
    probs = rg.online_problems(_GOOD)
    assert any("ruleset ACTIVO" in p for p in probs), probs


def test_online_insufficient_reviews_red(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "t")
    ruleset = [{"id": _GOOD["ruleset"]["id"], "enforcement": "active"}]
    prot = {
        "required_pull_request_reviews": {
            "required_approving_review_count": 0,
            "dismiss_stale_reviews": False,
            "require_last_push_approval": False,
            "require_code_owner_reviews": False,
        },
        "required_status_checks": {"strict": False},
    }
    monkeypatch.setattr(rg, "_gh_get", _mock_gh({"/rulesets": (ruleset, None), "/protection": (prot, None), "/collaborators": ([{}, {}], None)}))  # fmt: skip
    probs = rg.online_problems(_GOOD)
    assert any("aprobaciones" in p for p in probs), probs
    assert any("dismiss stale" in p for p in probs), probs
    assert any("code-owner" in p for p in probs), probs


def test_online_single_collaborator_red(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "t")
    ruleset = [{"id": _GOOD["ruleset"]["id"], "enforcement": "active"}]
    prot = {
        "required_pull_request_reviews": {
            "required_approving_review_count": 1,
            "dismiss_stale_reviews": True,
            "require_last_push_approval": True,
            "require_code_owner_reviews": True,
        },
        "required_status_checks": {"strict": True},
    }
    monkeypatch.setattr(rg, "_gh_get", _mock_gh({"/rulesets": (ruleset, None), "/protection": (prot, None), "/collaborators": ([{"login": "jrebull"}], None)}))  # fmt: skip
    probs = rg.online_problems(_GOOD)
    assert any("revisor independiente" in p for p in probs), probs


def test_online_fully_provisioned_still_red_by_external_blocker(monkeypatch):
    # incluso con la API 'perfecta', external_blocker.open=true mantiene B291 rojo hasta la acción 10.2 humana
    monkeypatch.setenv("GH_TOKEN", "t")
    ruleset = [{"id": _GOOD["ruleset"]["id"], "enforcement": "active"}]
    prot = {
        "required_pull_request_reviews": {
            "required_approving_review_count": 1,
            "dismiss_stale_reviews": True,
            "require_last_push_approval": True,
            "require_code_owner_reviews": True,
        },
        "required_status_checks": {"strict": True},
    }
    monkeypatch.setattr(rg, "_gh_get", _mock_gh({"/rulesets": (ruleset, None), "/protection": (prot, None), "/collaborators": ([{"login": "jrebull"}, {"login": "reviewer2"}], None)}))  # fmt: skip
    probs = rg.online_problems(_GOOD)
    assert any("external_blocker" in p for p in probs), probs


def test_external_blocker_is_open_in_real_contract():
    assert _GOOD["external_blocker"]["open"] is True, "B291 debe seguir ABIERTO hasta la acción externa 10.2"
