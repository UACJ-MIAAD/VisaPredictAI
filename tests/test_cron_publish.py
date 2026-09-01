"""C4: publicación por allowlist — lo de la fase se stagea, lo extraño se ve, nada se barre."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import cron_publish as cp  # noqa: E402


def test_model_stage_publishes_its_artifacts_and_rejects_strays() -> None:
    dirty = [
        "reports/prospective/forecast_log.csv",
        "reports/prospective/web_forecasts_meta.json",
        "reports/governance/key_facts.json",
        "reports/latex/key_facts.tex",
        "reports/release/release_manifest.json",
        "dvc.lock",
        "reports/notas_sueltas.md",  # extraño: antes viajaba en silencio con `git add reports`
        "reports/campaign/experimento_tmp.csv",
    ]
    publish, reject = cp.partition(dirty, "model")
    assert "reports/prospective/forecast_log.csv" in publish
    assert "dvc.lock" in publish
    # A-01: el manifiesto tiene DUEÑO ÚNICO (stage release, paso bloqueante del cron) —
    # las fases model/eda lo RECHAZAN a gritos en vez de publicar uno a medio sellar.
    assert "reports/release/release_manifest.json" in reject
    assert "reports/notas_sueltas.md" in reject
    assert "reports/campaign/experimento_tmp.csv" in reject
    publish_r, _ = cp.partition(dirty, "release")
    assert publish_r == ["reports/release/release_manifest.json"]


def test_data_stage_owns_the_cleaning_ledger() -> None:
    publish, reject = cp.partition(
        ["data/raw/mexico.csv", "reports/governance/cleaning_ledger.json", "reports/governance/key_facts.json"],
        "data",
    )
    assert "reports/governance/cleaning_ledger.json" in publish  # out del stage panel
    assert "data/raw/mexico.csv" in publish
    assert "reports/governance/key_facts.json" in reject  # eso lo publica el bloque de modelado


def test_eda_stage_covers_galleries_reports_and_tex_figures() -> None:
    dirty = [
        "reports/eda/eda_facts.json",
        "reports/eda/gallery/en/dark/g01_panel.png",
        "reports/fe/fe_report.pdf",
        "reports/latex/fe_facts.tex",
        "reports/latex/Figures/eda3_g01.pdf",
        "reports/latex/ProyectoI_VisaPredictAI.tex",  # el .tex del deliverable NO lo toca el cron
    ]
    publish, reject = cp.partition(dirty, "eda")
    assert len(publish) == 5
    assert reject == ["reports/latex/ProyectoI_VisaPredictAI.tex"]


def test_out_of_scope_paths_are_ignored_entirely() -> None:
    publish, reject = cp.partition(["ante/lib/x.py", "tools/foo.py"], "model")
    assert publish == [] and reject == []


def test_every_stage_allowlist_covers_dvc_lock_where_it_commits() -> None:
    for stage in ("data", "model", "eda"):
        publish, _ = cp.partition(["dvc.lock"], stage)
        assert publish == ["dvc.lock"], f"{stage} debe poder registrar el lock (gate E2)"


def test_data_stage_owns_the_mega_audit_report_it_regenerates() -> None:
    """A7.3-R1: la fase de datos CORRE `pipeline.mega_audit` como gate de calidad
    (paso 2 del rebuild), así que el informe que ese comando reescribe es suyo y
    debe publicarse con el commit de datos.

    Estar RASTREADO y fuera del allowlist es la peor combinación posible: el
    publicador lo deja sin stagear (correcto: nunca barre lo inesperado) y el
    `git pull --rebase origin main` que viene justo después se niega a correr con
    el árbol sucio. Eso tumbó el run 33443890067 con exit 128 ANTES del push.
    """
    publish, reject = cp.partition(["reports/governance/mega_audit_report.md"], "data")
    assert publish == ["reports/governance/mega_audit_report.md"]
    assert reject == []


def test_data_stage_leaves_no_tracked_stray_on_the_self_heal_rebuild() -> None:
    """Regresión conductual del run 33443890067 (auto-reparación A3, ingesta manual
    de 2026-08 y 2026-09 ya en S3): con el conjunto sucio EXACTO que produjo aquel
    rebuild, la fase de datos no puede dejar NINGÚN rechazado — cualquiera de ellos
    rompe el rebase posterior antes del push.
    """
    dirty = [
        "data/processed/bulletins.json",
        "data/processed/visa_panel_long.csv",
        "data/raw/china_family_visa_backlog_timecourse.csv",
        "data/raw/china_visa_backlog_timecourse.csv",
        "data/raw/dv_visa_rank_timecourse.csv",
        "data/raw/india_family_visa_backlog_timecourse.csv",
        "data/raw/india_visa_backlog_timecourse.csv",
        "data/raw/mexico_family_visa_backlog_timecourse.csv",
        "data/raw/mexico_visa_backlog_timecourse.csv",
        "data/raw/philippines_family_visa_backlog_timecourse.csv",
        "data/raw/philippines_visa_backlog_timecourse.csv",
        "data/raw/row_family_visa_backlog_timecourse.csv",
        "data/raw/row_visa_backlog_timecourse.csv",
        "dvc.lock",
        "reports/governance/cleaning_ledger.json",
        "reports/governance/mega_audit_report.md",
    ]
    publish, reject = cp.partition(dirty, "data")
    assert reject == [], f"la fase de datos dejaría sucio el árbol: {reject}"
    assert sorted(publish) == sorted(dirty)


def test_governance_artifacts_of_other_phases_are_still_rejected_by_data() -> None:
    """El ensanche de A7.3-R1 es NOMINAL, no un `reports/governance/` completo: los
    artefactos que publica el bloque de modelado siguen rechazados en la fase de
    datos (si no, un commit de datos los adelantaría a medio generar).
    """
    _, reject = cp.partition(
        [
            "reports/governance/key_facts.json",
            "reports/governance/champion_challenger.json",
            "reports/governance/promotion_decision.json",
        ],
        "data",
    )
    assert len(reject) == 3
