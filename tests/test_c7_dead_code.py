"""C7: el respaldo `sqrt(h)` de las bandas queda retirado, y no puede volver.

La banda de predicción del demostrador se derivaba de dos formas distintas: las cuantilas
empíricas por horizonte del ledger, y —si faltaba el archivo o la celda— un `sqrt(h)`
heredado con un escalar fijo. No eran equivalentes: el segundo infra-cubre, y servirlo en
silencio publicaba intervalos que no son los que el sistema declara.

Ahora `pi_scale_by_h.json` es obligatorio y una celda ausente detiene el build.
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
GENERADOR = ROOT / "experiments" / "generate_web_forecasts.py"


def _modulo():
    """El generador importado como lo hace el propio repo (sys.path[0] = experiments/)."""
    import sys

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if str(ROOT / "experiments") not in sys.path:
        sys.path.insert(0, str(ROOT / "experiments"))
    import generate_web_forecasts

    return generate_web_forecasts


class TestTheFallbackIsGone:
    def test_the_generator_no_longer_names_the_legacy_heuristic(self) -> None:
        """Anti-resurrección: ni el nombre del método ni el escalar vuelven al generador."""
        src = GENERADOR.read_text(encoding="utf-8")
        for muerto in ("sqrt_h", "BAND80_RATIO", "math.sqrt"):
            assert muerto not in src, f"el generador vuelve a mencionar {muerto}"

    def test_no_call_to_sqrt_survives_in_the_band_path(self) -> None:
        """Comprobado sobre el AST: un docstring que lo mencione no es una llamada."""
        tree = ast.parse(GENERADOR.read_text(encoding="utf-8"))
        llamadas = {
            n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
        }
        assert "sqrt" not in llamadas

    def test_the_band_scales_are_no_longer_optional(self) -> None:
        """La firma dejó de admitir `None`: no hay camino sin escalas."""
        tree = ast.parse(GENERADOR.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in {"_band_halfwidths", "_load_pi_scales"}:
                fuente = ast.unparse(node.args) + ast.unparse(node.returns or ast.Constant(None))
                assert "dict | None" not in fuente, f"{node.name} sigue admitiendo escalas ausentes"


class TestMissingScalesStopTheBuild:
    def test_a_missing_file_aborts_instead_of_estimating(self, tmp_path, monkeypatch) -> None:
        mod = _modulo()
        monkeypatch.setattr(mod, "REPORTS", tmp_path)
        with pytest.raises(SystemExit, match="pi_scale_by_h"):
            mod._load_pi_scales()

    def test_an_empty_scales_object_aborts(self, tmp_path, monkeypatch) -> None:
        mod = _modulo()
        (tmp_path / "prospective").mkdir()
        (tmp_path / "prospective" / "pi_scale_by_h.json").write_text(json.dumps({"scales": {}}))
        monkeypatch.setattr(mod, "REPORTS", tmp_path)
        with pytest.raises(SystemExit, match="ninguna escala"):
            mod._load_pi_scales()

    @pytest.mark.parametrize(
        ("scales", "porque"),
        [
            ({"FAD": {"80": {"1": 0.5}}}, "falta el nivel 95"),
            ({"FAD": {"95": {"1": 1.0}}}, "falta el nivel 80"),
            ({"FAD": {"80": {"2": 0.5}, "95": {"2": 1.0}}}, "falta el horizonte pedido"),
            ({"DFF": {"80": {"1": 0.5}, "95": {"1": 1.0}}}, "falta la tabla pedida"),
        ],
    )
    def test_a_missing_cell_aborts_and_says_which(self, scales: dict, porque: str) -> None:
        mod = _modulo()
        with pytest.raises(SystemExit, match="sin escala de banda"):
            mod._band_halfwidths(1, 100.0, "FAD", scales)

    def test_a_present_cell_is_used_verbatim(self) -> None:
        mod = _modulo()
        scales = {"FAD": {"80": {"3": 0.25}, "95": {"3": 2.0}}}
        h80, h95, metodo = mod._band_halfwidths(3, 100.0, "FAD", scales)
        assert (h80, h95, metodo) == (25.0, 200.0, "q_h")


class TestTheLiveScalesCoverWhatWePublish:
    def test_every_table_level_and_horizon_the_product_emits_has_a_cell(self) -> None:
        """Si esto falla, el archivo vivo dejó de cubrir el horizonte publicado."""
        import sys

        sys.path.insert(0, str(ROOT))
        from vp_model import config

        scales = json.loads((ROOT / "reports/prospective/pi_scale_by_h.json").read_text())["scales"]
        faltan = [
            f"{t}/{lvl}/h{h}"
            for t in config.TABLES
            for lvl in ("80", "95")
            for h in range(1, 13)
            if str(h) not in scales.get(t, {}).get(lvl, {})
        ]
        assert not faltan, f"celdas sin escala: {faltan}"
