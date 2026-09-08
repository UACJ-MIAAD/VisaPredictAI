"""C4: errores tipados de la capa de datos y trinquete honesto de deuda.

Lo que estas pruebas fijan es una frontera: un fallo ESPERADO (la fuente cae, un
boletín trae una tabla ilegible) se reporta y deja avanzar al resto; un defecto de
programación NO se disfraza de «mes perdido», escapa y pone el proceso en rojo.
"""

from __future__ import annotations

import ast
import json
import logging
import pathlib
import textwrap

import pandas as pd
import pytest

from vp_data import fetchers
from vp_data.errors import PARSE_FAILURES, FetchError, ParseError, SourceBlockedError

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def repo_root() -> pathlib.Path:
    return ROOT


class TestTypedErrors:
    def test_fetch_error_keeps_url_status_and_permanence(self) -> None:
        err = FetchError("https://x/y", "HTTP 404", status=404, permanent=True)
        assert err.url == "https://x/y"
        assert err.status == 404
        assert err.permanent is True
        assert "https://x/y" in str(err)

    def test_source_blocked_is_a_permanent_fetch_error(self) -> None:
        err = SourceBlockedError("https://x", status=403)
        assert isinstance(err, FetchError)
        assert err.permanent is True  # ningún backoff resuelve un desafío del WAF
        assert err.status == 403

    def test_parse_error_keeps_stage_source_month_country_and_cause(self) -> None:
        try:
            try:
                raise ValueError("columna ausente")
            except ValueError as exc:
                raise ParseError(
                    "tablas del panel", "2026-09.html", str(exc), month="2026-09", country="mexico"
                ) from exc
        except ParseError as err:
            assert err.stage == "tablas del panel"
            assert err.source == "2026-09.html"
            assert err.month == "2026-09"
            assert err.country == "mexico"
            assert isinstance(err.__cause__, ValueError)  # la causa nunca se pierde
            assert err.context == {
                "stage": "tablas del panel",
                "source": "2026-09.html",
                "month": "2026-09",
                "country": "mexico",
            }

    def test_the_reporter_line_does_not_truncate_or_mix_the_context(self) -> None:
        largo = "una causa larguísima " * 8
        err = ParseError("lectura", "2001-12.html", largo.strip(), month="2001-12")
        line = err.describe()
        assert largo.strip() in line  # nada de `str(exc)[:60]`
        assert "etapa=lectura" in line and "fuente=2001-12.html" in line and "mes=2001-12" in line
        assert "país=" not in line  # lo que no se conoce, no se inventa

    def test_compatibility_import_from_fetchers_still_works(self) -> None:
        assert fetchers.FetchError is FetchError
        assert fetchers.SourceBlockedError is SourceBlockedError

    def test_programming_defects_are_not_expected_failures(self) -> None:
        # La frontera de C4, escrita como aserción: estos NO se capturan.
        for tipo in (AttributeError, TypeError, NameError, RuntimeError, KeyboardInterrupt, SystemExit):
            assert not issubclass(tipo, PARSE_FAILURES) if isinstance(PARSE_FAILURES, type) else True
            assert tipo not in PARSE_FAILURES

    def test_keyboard_interrupt_and_system_exit_escape_the_expected_handler(self) -> None:
        for tipo in (KeyboardInterrupt, SystemExit):
            with pytest.raises(tipo):
                try:
                    raise tipo()
                except PARSE_FAILURES:  # pragma: no cover - no debe atrapar
                    pytest.fail(f"{tipo.__name__} fue absorbido")


class TestScrapersCatchOnlyExpectedFailures:
    ARCHIVOS = (
        "pipeline/scrape_all.py",
        "pipeline/scrape_visa_bulletins.py",
        "pipeline/scrape_family_visa_bulletins.py",
        "pipeline/scrape_dv_visa_bulletins.py",
    )

    def _handlers(self, repo_root, path):
        tree = ast.parse((repo_root / path).read_text())
        return [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]

    def test_no_scraper_catches_bare_exception_any_more(self, repo_root) -> None:
        for path in self.ARCHIVOS:
            for handler in self._handlers(repo_root, path):
                node = handler.type
                assert node is not None, f"{path}: `except:` desnudo"
                names = node.elts if isinstance(node, ast.Tuple) else [node]
                for n in names:
                    if isinstance(n, ast.Name):
                        assert n.id not in {"Exception", "BaseException"}, f"{path}: captura amplia viva"

    def test_the_failure_list_carries_structured_context(self, repo_root) -> None:
        for path in self.ARCHIVOS:
            src = (repo_root / path).read_text()
            assert "ParseError(" in src, f"{path}: no construye contexto"
            assert "str(exc)[:60]" not in src, f"{path}: sigue recortando la causa"


def _suppressions(counts: dict[str, int]) -> int:
    """Las supresiones de CUALQUIER regla. C8 (#51) las separó por regla y retiró el total
    agregado porque mezclaba naturalezas distintas; estas comprobaciones solo necesitan saber
    que NO se contó nada, así que suman aquí y no en el trinquete."""
    return sum(v for k, v in counts.items() if k.startswith("noqa_"))


class TestDebtCheckerCountsRealCode:
    def _count(self, source: str) -> dict[str, int]:
        from tools.check_debt import count_file

        return count_file(textwrap.dedent(source))

    def test_ignores_the_marker_inside_a_docstring(self) -> None:
        c = self._count('''
            """Este módulo habla de except Exception y de # noqa sin usarlos."""
            x = 1
        ''')
        assert c["except_exception"] == 0
        assert _suppressions(c) == 0

    def test_ignores_the_marker_inside_a_string_literal(self) -> None:
        # El falso positivo exacto que el propio checker se causaba.
        c = self._count("""
            def f(line):
                if "except Exception" in line:
                    return "# noqa: BLE001"
                return None
        """)
        assert c["except_exception"] == 0
        assert _suppressions(c) == 0

    def test_a_comment_that_merely_mentions_a_directive_is_not_a_suppression(self) -> None:
        c = self._count("""
            # la política pide noqa: BLE001 en toda captura amplia
            x = 1
        """)
        assert _suppressions(c) == 0

    def test_counts_a_real_handler_written_across_several_lines(self) -> None:
        c = self._count("""
            try:
                pass
            except (
                ValueError,
                Exception,
            ):
                pass
        """)
        assert c["except_exception"] == 1
        assert c["except_exception_unjustified"] == 1

    def test_a_justification_anywhere_in_the_handler_counts(self) -> None:
        c = self._count("""
            try:
                pass
            except Exception:  # noqa: BLE001 -- rollback garantizado
                pass
        """)
        assert c["except_exception"] == 1
        assert c["except_exception_unjustified"] == 0

    def test_a_double_dash_no_longer_passes_as_a_justification(self) -> None:
        # El regex viejo (`noqa|—|--`) daba por justificada casi cualquier línea comentada.
        c = self._count("""
            try:
                pass
            except Exception:  # amplio a propósito -- rollback
                pass
        """)
        assert c["except_exception_unjustified"] == 1

    def test_an_unjustified_broad_catch_turns_the_gate_red(self, tmp_path, monkeypatch) -> None:
        import tools.check_debt as cd

        # La baseline se deriva de METRICS: así el contrato de claves lo fija el propio módulo
        # y añadir una métrica (C8 añadió cinco) no vuelve a romper esta prueba.
        base = tmp_path / "baseline.json"
        base.write_text(json.dumps(dict.fromkeys(cd.METRICS, 0)) + "\n")
        monkeypatch.setattr(cd, "BASELINE", base)
        peor = dict.fromkeys(cd.METRICS, 0) | {"except_exception": 1, "except_exception_unjustified": 1}
        monkeypatch.setattr(cd, "counts", lambda: peor)
        monkeypatch.setattr("sys.argv", ["check_debt.py"])
        assert cd.main() == 1

    @pytest.mark.parametrize(
        "raw",
        [
            '{"except_exception": 1}',  # faltan claves
            '{"except_exception": 0, "except_exception_unjustified": 0, "type_ignore": 0,'
            ' "noqa": 0, "todo_class": 0, "extra": 1}',  # clave de más
            '{"except_exception": true, "except_exception_unjustified": 0, "type_ignore": 0,'
            ' "noqa": 0, "todo_class": 0}',  # booleano
            '{"except_exception": -1, "except_exception_unjustified": 0, "type_ignore": 0,'
            ' "noqa": 0, "todo_class": 0}',  # negativo
            '{"except_exception": "3", "except_exception_unjustified": 0, "type_ignore": 0,'
            ' "noqa": 0, "todo_class": 0}',  # cadena
            "[]",  # ni siquiera un objeto
        ],
    )
    def test_a_malformed_baseline_fails_closed(self, raw: str) -> None:
        from tools.check_debt import load_baseline

        with pytest.raises(ValueError):
            load_baseline(raw)

    def test_the_real_baseline_is_valid(self, repo_root) -> None:
        from tools.check_debt import load_baseline

        assert load_baseline((repo_root / "docs" / "debt_baseline.json").read_text())


class TestSkippedTablesAreAudible:
    def test_an_unusable_table_logs_country_month_and_cause(self, caplog) -> None:
        from vp_data.extract import extract_country_data

        # Falta `table_type`: la selección lanza KeyError y la tabla se omite. (La
        # cabecera duplicada revienta MÁS ABAJO, fuera del `try`; es el defecto latente
        # que C2 documentó y preservó, y C4 no lo toca.)
        df = pd.DataFrame(
            [["EB-1", "01JAN20", "2026-09-01"]],
            columns=["category", "mexico", "visa_bulletin_date"],
        )
        with caplog.at_level(logging.WARNING):
            out = extract_country_data("mexico", [df], level_col="EB_level", classifier=lambda s: str(s))
        assert out.empty  # la tabla se omite, como siempre
        mensajes = " ".join(r.getMessage() for r in caplog.records)
        assert "mexico" in mensajes  # …pero ya no en silencio
        assert "2026-09" in mensajes
        assert "columnas de país" in mensajes
