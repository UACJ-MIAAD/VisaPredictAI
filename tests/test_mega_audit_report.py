"""C3: la auditoría es reejecutable — su estado vive en un objeto, no en el módulo.

`L` y `FLAGS` eran globales: dos invocaciones en el mismo proceso acumulaban líneas y banderas,
así que la auditoría solo era correcta recién arrancado el intérprete. Con `AuditReport`, cada
corrida parte de cero. Aquí se prueban tres cosas: que las severidades y el veredicto no
cambiaron, que un crítico rompe y un WARN/INFO no, y que dos corridas no se contaminan.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # el módulo importa vp_data.config
_spec = importlib.util.spec_from_file_location("mega_audit_c3", ROOT / "pipeline" / "mega_audit.py")
assert _spec is not None and _spec.loader is not None
ma = importlib.util.module_from_spec(_spec)
sys.modules["mega_audit_c3"] = ma
_spec.loader.exec_module(ma)


def _panel(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    for col in ("bulletin_date", "priority_date"):
        df[col] = pd.to_datetime(df[col])
    return df


def _row(**kw) -> dict:
    base = dict(
        country="mexico",
        block="family",
        category="F1",
        table="FAD",
        bulletin_date="2024-01-01",
        priority_date="2001-01-01",
        status="F",
        raw_value="01JAN01",
        days_since_base=9497,
        raw_category="F1",
    )
    base.update(kw)
    return base


# --- el objeto: severidades, veredicto y código de salida --------------------------


def test_a_fresh_report_starts_empty() -> None:
    rep = ma.AuditReport()
    assert rep.lines == [] and rep.flags == []
    assert rep.verdict == "APTO" and rep.exit_code == 0


def test_a_critical_blocks_and_the_rest_does_not() -> None:
    rep = ma.AuditReport()
    rep.flag("WARN", "algo raro")
    rep.flag("INFO", "algo curioso")
    assert rep.exit_code == 0 and rep.verdict == "APTO", "WARN/INFO no pueden bloquear"
    rep.flag("CRIT", "algo roto")
    assert rep.exit_code == 1 and rep.verdict == "REQUIERE ATENCIÓN"


def test_an_unknown_severity_is_rejected() -> None:
    with pytest.raises(ValueError, match="severidad desconocida"):
        ma.AuditReport().flag("FATAL", "no existe")


def test_render_joins_lines_exactly_as_before() -> None:
    rep = ma.AuditReport()
    rep.add("a", "b")
    rep.add("c")
    assert rep.render() == "a\nb\nc"


# --- dimensiones que deben marcar CRIT ---------------------------------------------


def test_d6_flags_duplicate_keys_as_critical() -> None:
    dup = _panel([_row(), _row()])  # misma (país, cat, tabla, mes) dos veces
    rep = ma.AuditReport()
    ma.d6_keys(rep, dup)
    assert rep.by_severity("CRIT") and rep.exit_code == 1
    assert "duplicadas" in rep.by_severity("CRIT")[0]


def test_d2_flags_unexplained_missing_months_as_critical() -> None:
    hueco = _panel([_row(bulletin_date="2024-01-01"), _row(bulletin_date="2024-06-01", priority_date="2001-06-01")])
    rep = ma.AuditReport()
    ma.d2_completeness(rep, hueco)
    crit = rep.by_severity("CRIT")
    assert crit and "meses ausentes no explicados" in crit[0]
    assert rep.exit_code == 1


@pytest.fixture()
def fuentes_vacias(tmp_path: Path, monkeypatch) -> Path:
    """Las 10 fuentes que `d10` reconcilia, con cabecera y sin filas."""
    raw = tmp_path / "raw"
    raw.mkdir()
    cabecera = "level,priority_date,visa_bulletin_date,table_type,raw_value,status,visa_wait_time,raw_category\n"
    for slug in ma.COUNTRIES:
        for suf in ("", "_family"):
            (raw / f"{slug}{suf}_visa_backlog_timecourse.csv").write_text(cabecera, encoding="utf-8")
    monkeypatch.setattr(ma, "RAW", raw)
    return raw


def test_d10_flags_a_panel_richer_than_its_sources_as_critical(fuentes_vacias: Path) -> None:
    """Un panel con más filas que las fuentes es imposible: eso es CRIT."""
    rep = ma.AuditReport()
    ma.d10_reconcile(rep, _panel([_row(), _row(bulletin_date="2024-02-01")]))
    crit = rep.by_severity("CRIT")
    assert crit and "imposible" in crit[0]
    assert rep.exit_code == 1


# --- dimensiones cuya severidad NO se toca en C3 ------------------------------------


def test_d8_keeps_its_informational_severity() -> None:
    """D8 marca INFO, no CRIT. Promoverlo es un cambio de política con su propio RED."""
    src = (ROOT / "pipeline" / "mega_audit.py").read_text(encoding="utf-8")
    bloque = src.split("def d8_dff_fad")[1].split("def d9_jumps")[0]
    assert 'flag("INFO"' in bloque and 'flag("CRIT"' not in bloque


def test_d9_keeps_its_informational_severity_and_threshold() -> None:
    src = (ROOT / "pipeline" / "mega_audit.py").read_text(encoding="utf-8")
    assert "def d9_jumps(rep: AuditReport, p, thresh_years=8)" in src, "el umbral de D9 no cambia"
    bloque = src.split("def d9_jumps")[1].split("def d10_reconcile")[0]
    assert 'flag("INFO"' in bloque and 'flag("CRIT"' not in bloque


# --- determinismo del inventario ----------------------------------------------------


def _inventario_sintetico() -> pd.DataFrame:
    """Inventario con MUCHOS empates en n_F: el caso que rompía la selección."""
    filas = []
    for pais in ("mexico", "india", "china", "philippines", "all_chargeability"):
        for cat in ("EB5_RURAL", "EB5_INFRA", "EB5_HIGHUNEMP", "F1"):
            for tabla in ("FAD", "DFF"):
                filas.append(
                    {
                        "country": pais,
                        "block": "employment" if cat.startswith("EB") else "family",
                        "category": cat,
                        "table": tabla,
                        "n": 53,
                        "n_F": 0,
                        "start": "2022-05",
                        "end": "2026-09",
                    }
                )
    filas.append(
        {
            "country": "mexico",
            "block": "family",
            "category": "F4",
            "table": "FAD",
            "n": 100,
            "n_F": 90,
            "start": "2001-12",
            "end": "2026-09",
        }
    )
    return pd.DataFrame(filas)


def _tabla_de(inv: pd.DataFrame) -> str:
    rep = ma.AuditReport()
    ma.d3_inventory(rep, inv)
    return rep.render()


@pytest.mark.parametrize("semilla", [1, 7, 42, 2026])
def test_the_shortest_series_table_ignores_input_order(semilla: int) -> None:
    """El MISMO inventario en otro orden debe dar exactamente las mismas 12 filas.

    Sin el desempate, `sort_values("n_F")` usa quicksort —no estable— y cada permutación
    elegía series distintas de entre las decenas empatadas en cero."""
    inv = _inventario_sintetico()
    assert _tabla_de(inv) == _tabla_de(inv.sample(frac=1.0, random_state=semilla))


def test_the_selection_is_ordered_by_the_full_semantic_key() -> None:
    inv = _inventario_sintetico()
    rep = ma.AuditReport()
    ma.d3_inventory(rep, inv)
    filas = [ln for ln in rep.lines if ln.startswith("| ") and "→" in ln]
    claves = [tuple(c.strip() for c in ln.strip("|").split("|")[:3]) for ln in filas]
    assert claves == sorted(claves), "las series empatadas deben salir en orden de clave"
    assert len(claves) == 12


def test_the_tiebreak_is_declared_and_stable() -> None:
    src = (ROOT / "pipeline" / "mega_audit.py").read_text(encoding="utf-8")
    assert 'sort_values(["n_F", "country", "block", "category", "table"], kind="stable")' in src
    assert 'sort_values("n_F").head' not in src, "el orden inestable volvió"


# --- aislamiento entre corridas -----------------------------------------------------


def test_two_consecutive_runs_do_not_accumulate() -> None:
    p = _panel([_row(), _row(bulletin_date="2024-02-01", priority_date="2001-02-01")])
    inv = ma.series_table(p)
    primero = ma.AuditReport()
    ma.d3_inventory(primero, inv)
    segundo = ma.AuditReport()
    ma.d3_inventory(segundo, inv)
    assert primero.lines == segundo.lines
    assert len(segundo.lines) == len(primero.lines), "la segunda corrida no puede heredar líneas"


def test_a_failed_run_does_not_leak_into_the_next_one() -> None:
    sucio = ma.AuditReport()
    ma.d6_keys(sucio, _panel([_row(), _row()]))  # deja un CRIT
    assert sucio.exit_code == 1
    limpio = ma.AuditReport()
    ma.d6_keys(limpio, _panel([_row(), _row(bulletin_date="2024-02-01")]))
    assert limpio.flags == [] and limpio.exit_code == 0, "el CRIT anterior se filtró"


def test_two_interleaved_reports_stay_independent() -> None:
    a, b = ma.AuditReport(), ma.AuditReport()
    a.add("uno")
    b.add("dos")
    a.flag("CRIT", "solo de a")
    b.add("tres")
    assert a.lines == ["uno"] and b.lines == ["dos", "tres"]
    assert a.exit_code == 1 and b.exit_code == 0


def test_the_module_keeps_no_mutable_audit_state() -> None:
    """Si `L` o `FLAGS` vuelven al módulo, la reejecución se rompe otra vez."""
    assert not hasattr(ma, "L") and not hasattr(ma, "FLAGS")
    src = (ROOT / "pipeline" / "mega_audit.py").read_text(encoding="utf-8")
    assert "\nL: list" not in src and "\nFLAGS: list" not in src


def test_build_report_returns_a_new_instance_every_time(fuentes_vacias: Path) -> None:
    # el informe completo recorre las 11 dimensiones: el panel necesita ambas tablas
    p = _panel([_row(), _row(table="DFF", priority_date="2001-03-01", days_since_base=9556)])
    inv = ma.series_table(p)
    uno = ma.build_report(p, inv)
    dos = ma.build_report(p, inv)
    assert uno is not dos, "cada invocación debe traer su propio informe"
    assert uno.lines == dos.lines and uno.flags == dos.flags
    assert uno.render() == dos.render(), "dos corridas sobre el mismo panel dan el mismo texto"


# --- el entrypoint no cambia --------------------------------------------------------


def test_the_entrypoint_and_its_blocking_failure_are_unchanged() -> None:
    src = (ROOT / "pipeline" / "mega_audit.py").read_text(encoding="utf-8")
    assert "raise SystemExit(main())" in src
    assert 'OUT = Path("reports/governance/mega_audit_report.md")' in src
    mk = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "mega_audit" in mk, "make audit debe seguir invocando el mismo módulo"
