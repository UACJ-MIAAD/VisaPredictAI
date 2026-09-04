"""B1: el manifiesto de release — checksums, criticidad, id determinista, falta crítica aborta."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "experiments"))

import build_release_manifest as brm  # noqa: E402


def _seed_tree(root: Path, skip: set[str] | None = None) -> None:
    """Árbol mínimo con todos los artefactos no-opcionales del spec."""
    skip = skip or set()
    for rel, crit in brm.artifact_spec():
        if crit == "optional" or rel in skip:
            continue
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"contenido:{rel}\n")
    # panel con esquema mínimo para panel_vintage/panel_hash, y JSONs de verdad
    panel = root / "data" / "processed" / "visa_panel_long.csv"
    panel.write_text("bulletin_date,days_since_base\n2026-07-01,100\n")
    if "reports/governance/key_facts.json" not in skip:
        (root / "reports" / "governance" / "key_facts.json").write_text('{"n_obs": 5, "n_months": 1}')
    (root / "reports" / "governance" / "champion_manifest.json").write_text('{"FAD": {"models": ["theta"]}}')


def test_missing_critical_aborts(tmp_path) -> None:
    _seed_tree(tmp_path, skip={"reports/governance/key_facts.json"})
    with pytest.raises(SystemExit, match="critical:reports/governance/key_facts.json"):
        brm.build(tmp_path)


def test_missing_required_aborts(tmp_path) -> None:
    _seed_tree(tmp_path, skip={"reports/fe/fe_facts.json"})
    with pytest.raises(SystemExit, match="required:reports/fe/fe_facts.json"):
        brm.build(tmp_path)


def test_empty_critical_aborts(tmp_path) -> None:
    """D2-A: un critical de 0 bytes es un bloqueante inválido — el productor aborta."""
    _seed_tree(tmp_path)
    (tmp_path / "reports" / "prospective" / "web_forecasts.csv").write_bytes(b"")
    with pytest.raises(SystemExit, match=r"vacíos \(0 bytes\).*critical:reports/prospective/web_forecasts\.csv"):
        brm.build(tmp_path)


def test_empty_required_aborts(tmp_path) -> None:
    """D2-A: un required de 0 bytes también aborta, con su criticidad y ruta en el diagnóstico."""
    _seed_tree(tmp_path)
    (tmp_path / "reports" / "prospective" / "forecast_scorecard_shadow.csv").write_bytes(b"")
    with pytest.raises(
        SystemExit, match=r"vacíos \(0 bytes\).*required:reports/prospective/forecast_scorecard_shadow\.csv"
    ):
        brm.build(tmp_path)


def test_nonempty_blocking_artifacts_are_accepted(tmp_path) -> None:
    """Comportamiento intacto para bloqueantes con contenido: se emite el corte completo."""
    _seed_tree(tmp_path)
    m = brm.build(tmp_path)
    blocking = [e for e in m["artifacts"] if e["criticality"] in ("critical", "required")]
    assert blocking and all(e["size"] > 0 for e in blocking)
    assert m["n_artifacts"] == len(m["artifacts"])


def test_missing_blocking_keeps_previous_diagnostic(tmp_path) -> None:
    """Un archivo AUSENTE sigue abortando por la vía previa (ausentes), no por la nueva."""
    _seed_tree(tmp_path, skip={"reports/prospective/web_forecasts.csv"})
    with pytest.raises(SystemExit, match="ausentes") as exc:
        brm.build(tmp_path)
    assert "vacíos" not in str(exc.value)


def test_empty_optional_keeps_current_behaviour(tmp_path) -> None:
    """D2-A no toca los opcionales: un optional de 0 bytes se incluye y no aborta."""
    _seed_tree(tmp_path)
    pdf = tmp_path / "reports" / "eda" / "eda_report.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"")
    m = brm.build(tmp_path)
    by_path = {e["path"]: e for e in m["artifacts"]}
    assert by_path["reports/eda/eda_report.pdf"]["size"] == 0
    assert "reports/eda/eda_report.pdf" not in m["missing_optional"]


def test_real_manifest_blocking_artifacts_exist_and_are_nonempty() -> None:
    """El corte commiteado cumple el contrato: todo critical/required existe y pesa > 0."""
    root = Path(__file__).resolve().parent.parent
    manifest = json.loads((root / "reports" / "release" / "release_manifest.json").read_text())
    blocking = [e for e in manifest["artifacts"] if e["criticality"] in ("critical", "required")]
    assert blocking, "el manifiesto real no declara artefactos bloqueantes"
    zero_in_manifest = [e["path"] for e in blocking if e["size"] == 0]
    absent = [e["path"] for e in blocking if not (root / e["path"]).exists()]
    empty_on_disk = [
        e["path"] for e in blocking if (root / e["path"]).exists() and (root / e["path"]).stat().st_size == 0
    ]
    assert zero_in_manifest == [] and absent == [] and empty_on_disk == []


def test_missing_optional_warns_and_omits(tmp_path) -> None:
    _seed_tree(tmp_path)
    m = brm.build(tmp_path)
    assert "reports/eda/eda_report.pdf" in m["missing_optional"]
    assert all(
        e["criticality"] in ("critical", "required") or e["path"].endswith(".png") is False for e in m["artifacts"]
    )


def test_release_id_is_deterministic_and_content_addressed(tmp_path) -> None:
    _seed_tree(tmp_path)
    a = brm.build(tmp_path)
    b = brm.build(tmp_path)
    assert a["release_id"] == b["release_id"]  # generated_at NO entra al id
    assert a["release_id"].startswith(a["panel_vintage"])
    (tmp_path / "reports" / "governance" / "key_facts.json").write_text("{}")
    c = brm.build(tmp_path)
    assert c["release_id"] != a["release_id"]  # un byte distinto ⇒ release distinto


def test_sha256_and_mime_are_real(tmp_path) -> None:
    _seed_tree(tmp_path)
    m = brm.build(tmp_path)
    by_path = {e["path"]: e for e in m["artifacts"]}
    kf = by_path["reports/governance/key_facts.json"]
    raw = (tmp_path / kf["path"]).read_bytes()
    assert kf["sha256"] == hashlib.sha256(raw).hexdigest()
    assert kf["size"] == len(raw)
    assert kf["mime"] == "application/json"
    assert by_path["data/processed/visa_panel_long.csv"]["mime"] == "text/csv"


def test_spec_covers_web_consumption_contract() -> None:
    """El spec cubre los 11 base + 72 PNG (4 variantes × 11 EDA + 7 FE) que baja el web."""
    spec = dict(brm.artifact_spec())
    assert spec["data/processed/visa_panel_long.csv"] == "critical"
    assert spec["reports/prospective/web_forecasts.csv"] == "critical"
    assert spec["reports/governance/key_facts.json"] == "critical"
    pngs = [p for p in spec if p.endswith(".png")]
    assert len(pngs) == (11 + 7) * 4
    assert all(spec[p] == "optional" for p in pngs)


# ======================================================================================
# D4: una sola identidad de release, sin ciclo tarjeta ⇄ manifiesto
# ======================================================================================
import datetime  # noqa: E402
import re  # noqa: E402

import build_model_card as bmc  # noqa: E402

from tools import check_contracts as cc  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
STUB_BODY = "# tarjeta de prueba\n\ncontenido estable del corte\n\n"


def _stub(release_id: str) -> str:
    """Tarjeta mínima con el MISMO contrato estructural que la real."""
    return f"{STUB_BODY}```\n{bmc.RELEASE_ID_FIELD}{release_id}\n```\n"


def _emit(root):
    """Emite manifiesto + tarjeta en un árbol sembrado y devuelve (manifiesto, texto de la tarjeta)."""
    manifest = brm.build(root, card_renderer=_stub)
    (root / "reports" / "release").mkdir(parents=True, exist_ok=True)
    (root / "reports" / "release" / "release_manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest, (root / brm.CARD_REL).read_text()


# ------------------------------------------------------------------ render puro y sentinel
def test_render_is_pure_and_carries_exactly_one_structural_marker() -> None:
    normalized = bmc.render(bmc.RELEASE_ID_SENTINEL)
    assert normalized.count(bmc.RELEASE_ID_SENTINEL) == 1
    assert len(re.findall(bmc.RELEASE_ID_RE, normalized)) == 1
    real = bmc.render("2026-09-abcdef123456")
    assert bmc.extract_release_id(real) == "2026-09-abcdef123456"
    assert bmc.normalize(real) == normalized  # el sentinel se restaura exactamente
    assert bmc.render("2026-09-abcdef123456") == real  # pura: mismo id ⇒ mismos bytes


def test_model_card_is_no_longer_its_own_emitter() -> None:
    """Un solo emisor: la tarjeta se niega a emitirse por su cuenta."""
    assert bmc.main() != 0


def test_make_model_card_delegates_to_release_manifest() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text()
    assert re.search(r"(?m)^model-card: release-manifest", makefile)
    assert "$(PY) experiments/build_model_card.py" not in makefile


def test_cron_no_longer_emits_the_card_early() -> None:
    cron = (REPO_ROOT / ".github" / "workflows" / "freeze_and_rebuild.yml").read_text()
    assert "python experiments/build_model_card.py" not in cron
    assert "python experiments/build_release_manifest.py" in cron  # sigue en el gate bloqueante


def test_release_allowlist_names_both_files_and_no_broad_path() -> None:
    from tools.cron_publish import ALLOWLIST

    assert set(ALLOWLIST["release"]) == {"reports/release/release_manifest.json", brm.CARD_REL}
    assert not any(p.endswith("/") for p in ALLOWLIST["release"])


def test_validator_field_matches_the_emitter_field() -> None:
    """El validador es stdlib puro y replica el patrón: aquí se fija que no derive."""
    assert cc.RELEASE_ID_FIELD == bmc.RELEASE_ID_FIELD
    assert cc.IDENTITY_SCHEMA == brm.IDENTITY_SCHEMA


# ------------------------------------------------------------------ identidad y emisión
def test_card_declares_its_own_release_id_and_manifest_stores_final_bytes(tmp_path) -> None:
    _seed_tree(tmp_path)
    manifest, card = _emit(tmp_path)
    assert bmc.extract_release_id(card) == manifest["release_id"]
    entry = next(e for e in manifest["artifacts"] if e["path"] == brm.CARD_REL)
    assert entry["sha256"] == hashlib.sha256(card.encode()).hexdigest()
    assert entry["size"] == len(card.encode())
    assert manifest["release_id"].endswith(manifest["identity"]["digest"][:12])


def test_identity_is_idempotent_with_two_controlled_timestamps(tmp_path, monkeypatch) -> None:
    """Dos sellos de tiempo DISTINTOS, mismo id, mismo digest y misma tarjeta."""
    _seed_tree(tmp_path)
    stamps = iter(
        [
            datetime.datetime(2026, 9, 4, 10, 0, 0, tzinfo=datetime.UTC),
            datetime.datetime(2027, 1, 31, 23, 59, 59, tzinfo=datetime.UTC),
        ]
    )

    class _FixedClock(datetime.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003 - firma de datetime.datetime.now
            return next(stamps)

    monkeypatch.setattr(brm.datetime, "datetime", _FixedClock)
    first, card_a = _emit(tmp_path)
    second, card_b = _emit(tmp_path)
    assert first["generated_at"] != second["generated_at"], "los sellos deben ser distintos"
    assert first["generated_at"].startswith("2026-09-04") and second["generated_at"].startswith("2027-01-31")
    assert first["release_id"] == second["release_id"]
    assert first["identity"]["digest"] == second["identity"]["digest"]
    assert card_a == card_b


def test_manifest_and_release_state_stay_out_of_their_own_digest(tmp_path) -> None:
    _seed_tree(tmp_path)
    manifest, _ = _emit(tmp_path)
    paths = {e["path"] for e in manifest["artifacts"]}
    assert "reports/release/release_manifest.json" not in paths
    assert not any(p.rsplit("/", 1)[-1] == "release-state.json" for p in paths)


def test_fresh_emission_passes_the_validator(tmp_path) -> None:
    _seed_tree(tmp_path)
    _emit(tmp_path)
    assert cc.release_identity_problems(tmp_path) == []


def test_removing_identity_from_a_d4_manifest_fails(tmp_path) -> None:
    """RED→GREEN: quitar `identity` de un corte D4 NO lo degrada a 'no verificable'; falla.

    El puente PRE-D4 es una excepción NOMINAL para un único corte legado, no una categoría.
    """
    _seed_tree(tmp_path)
    manifest, _ = _emit(tmp_path)
    assert cc.release_identity_problems(tmp_path) == []  # GREEN con identity
    del manifest["identity"]
    (tmp_path / "reports" / "release" / "release_manifest.json").write_text(json.dumps(manifest))
    problems = cc.release_identity_problems(tmp_path)  # RED sin identity
    assert problems and any("NO es el corte legado acreditado" in p for p in problems)


def test_legacy_bridge_accepts_only_the_accredited_cut() -> None:
    """El corte vivo (pre-D4) pasa por sus cuatro valores exactos, verificados contra el disco."""
    assert cc.release_identity_problems(REPO_ROOT) == []
    manifest = json.loads((REPO_ROOT / cc.MANIFEST_PATH).read_text())
    assert "identity" not in manifest, "si este corte ya trae identity, el puente sobra"
    assert hashlib.sha256((REPO_ROOT / cc.MANIFEST_PATH).read_bytes()).hexdigest() == cc.LEGACY_MANIFEST_SHA256
    assert hashlib.sha256((REPO_ROOT / cc.CARD_PATH).read_bytes()).hexdigest() == cc.LEGACY_CARD_SHA256
    assert manifest["release_id"] == cc.LEGACY_RELEASE_ID and manifest["panel_vintage"] == cc.LEGACY_VINTAGE


@pytest.mark.parametrize("field", ["release_id", "panel_vintage"])
def test_legacy_bridge_rejects_any_other_pre_d4_manifest(tmp_path, field: str) -> None:
    """Un manifiesto PRE-D4 distinto del acreditado falla, aunque sea válido en lo demás."""
    (tmp_path / "reports" / "release").mkdir(parents=True)
    (tmp_path / "reports" / "governance").mkdir(parents=True)
    (tmp_path / cc.CARD_PATH).write_text("# otra tarjeta\n")
    manifest = {"release_id": cc.LEGACY_RELEASE_ID, "panel_vintage": cc.LEGACY_VINTAGE, "artifacts": []}
    manifest[field] = "valor-distinto"
    (tmp_path / cc.MANIFEST_PATH).write_text(json.dumps(manifest))
    problems = cc.release_identity_problems(tmp_path)
    assert problems and "NO es el corte legado acreditado" in problems[0]


def test_duplicate_json_keys_in_the_manifest_are_rejected(tmp_path) -> None:
    (tmp_path / "reports" / "release").mkdir(parents=True)
    (tmp_path / "reports" / "governance").mkdir(parents=True)
    (tmp_path / cc.CARD_PATH).write_text("# tarjeta\n")
    (tmp_path / cc.MANIFEST_PATH).write_text('{"release_id": "a", "release_id": "b"}')
    problems = cc.release_identity_problems(tmp_path)
    assert problems and "clave JSON duplicada" in problems[0]


@pytest.mark.parametrize(
    "mutate,needle",
    [
        (lambda i: i.pop("digest"), "faltan"),
        (lambda i: i.update(extra="x"), "sobran"),
        (lambda i: i.update(schema=3), "no es el entero"),
        (lambda i: i.update(schema=True), "no es el entero"),
        (lambda i: i.update(card_path="otra/ruta.md"), "card_path"),
        (lambda i: i.update(sentinel="@@OTRO@@"), "sentinel"),
        (lambda i: i.update(digest="ABC"), "hex minúscula"),
        (lambda i: i.update(digest="F" * 64), "hex minúscula"),
        (lambda i: i.update(card_sha256_normalized="x" * 63), "hex minúscula"),
    ],
)
def test_malformed_identity_never_degrades(tmp_path, mutate, needle: str) -> None:
    """Un `identity` mal formado, incompleto o con otro schema FALLA; jamás se degrada."""
    _seed_tree(tmp_path)
    manifest, _ = _emit(tmp_path)
    mutate(manifest["identity"])
    (tmp_path / cc.MANIFEST_PATH).write_text(json.dumps(manifest))
    problems = cc.release_identity_problems(tmp_path)
    assert problems and any(needle in p for p in problems)


def test_identity_must_be_an_object(tmp_path) -> None:
    _seed_tree(tmp_path)
    manifest, _ = _emit(tmp_path)
    manifest["identity"] = ["no", "es", "objeto"]
    (tmp_path / cc.MANIFEST_PATH).write_text(json.dumps(manifest))
    assert any("debe ser un objeto" in p for p in cc.release_identity_problems(tmp_path))


def test_duplicate_artifact_paths_and_card_entries_are_rejected(tmp_path) -> None:
    _seed_tree(tmp_path)
    manifest, _ = _emit(tmp_path)
    card_entry = next(a for a in manifest["artifacts"] if a["path"] == cc.CARD_PATH)
    manifest["artifacts"].append(dict(card_entry))
    (tmp_path / cc.MANIFEST_PATH).write_text(json.dumps(manifest))
    problems = cc.release_identity_problems(tmp_path)
    assert any("ruta duplicada" in p for p in problems)
    assert any("exactamente una vez" in p for p in problems)


def test_release_id_must_be_vintage_plus_digest_prefix(tmp_path) -> None:
    """El id esperado es EXACTAMENTE f"{panel_vintage}-{digest[:12]}"."""
    _seed_tree(tmp_path)
    manifest, _ = _emit(tmp_path)
    assert manifest["release_id"] == f"{manifest['panel_vintage']}-{manifest['identity']['digest'][:12]}"
    manifest["panel_vintage"] = "1999-01"
    (tmp_path / cc.MANIFEST_PATH).write_text(json.dumps(manifest))
    assert any("!=" in p and "release_id" in p for p in cc.release_identity_problems(tmp_path))


# ------------------------------------------------------------------ tamper: todo debe caer
def _tamper(tmp_path, mutate_card=None, mutate_manifest=None):
    _seed_tree(tmp_path)
    manifest, card = _emit(tmp_path)
    if mutate_card:
        (tmp_path / brm.CARD_REL).write_text(mutate_card(card))
    if mutate_manifest:
        mutate_manifest(manifest)
        (tmp_path / "reports" / "release" / "release_manifest.json").write_text(json.dumps(manifest))
    return cc.release_identity_problems(tmp_path)


def test_missing_marker_is_rejected(tmp_path) -> None:
    problems = _tamper(tmp_path, mutate_card=lambda c: bmc.RELEASE_ID_RE.sub("otra_cosa: x", c))
    assert any("exactamente 1" in p for p in problems)


def test_duplicate_marker_is_rejected(tmp_path) -> None:
    problems = _tamper(tmp_path, mutate_card=lambda c: c + f"\n{bmc.RELEASE_ID_FIELD}2026-09-otro\n")
    assert any("2 veces" in p for p in problems)


def test_malformed_marker_is_rejected(tmp_path) -> None:
    problems = _tamper(tmp_path, mutate_card=lambda c: bmc.RELEASE_ID_RE.sub(f"{bmc.RELEASE_ID_FIELD}", c))
    assert problems  # sin valor, el patrón deja de casar: 0 marcadores


def test_accidental_global_substitution_is_rejected(tmp_path) -> None:
    """Si el id se cuela en otra parte del texto, la normalización deja de reproducirse."""

    def mutate(card: str) -> str:
        rid = bmc.extract_release_id(card)
        return card.replace(STUB_BODY, f"{STUB_BODY}nota: {rid}\n")

    problems = _tamper(tmp_path, mutate_card=mutate)
    assert any("TARJETA MANIPULADA" in p or "normalizada" in p for p in problems)


def test_manipulated_id_is_rejected(tmp_path) -> None:
    problems = _tamper(
        tmp_path, mutate_card=lambda c: bmc.RELEASE_ID_RE.sub(f"{bmc.RELEASE_ID_FIELD}2026-09-falsificado", c)
    )
    assert any("ID MANIPULADO" in p for p in problems)


def test_manipulated_card_body_is_rejected(tmp_path) -> None:
    problems = _tamper(tmp_path, mutate_card=lambda c: c.replace("contenido estable", "contenido ALTERADO"))
    assert any("TARJETA MANIPULADA" in p for p in problems)


def test_manipulated_artifact_hash_is_rejected(tmp_path) -> None:
    def mutate(manifest):
        for e in manifest["artifacts"]:
            if e["path"] == "reports/governance/key_facts.json":
                e["sha256"] = "0" * 64

    problems = _tamper(tmp_path, mutate_manifest=mutate)
    assert any("digest recomputado no coincide" in p for p in problems)


def test_manipulated_release_id_in_manifest_is_rejected(tmp_path) -> None:
    problems = _tamper(tmp_path, mutate_manifest=lambda m: m.__setitem__("release_id", "2026-09-000000000000"))
    assert any("ID MANIPULADO" in p or "no deriva del digest" in p for p in problems)


def test_absent_manifest_is_governed_by_its_contract_not_by_the_identity_check(tmp_path) -> None:
    """La identidad no juzga la PRESENCIA del manifiesto: eso lo hace su propio contrato."""
    assert cc.release_identity_problems(tmp_path) == []
    contract = REPO_ROOT / "vp_data" / "contracts" / "release_manifest.json"
    assert contract.exists(), "sin este contrato nadie exigiría que el manifiesto exista"
    assert json.loads(contract.read_text())["artifact"] == "reports/release/release_manifest.json"
