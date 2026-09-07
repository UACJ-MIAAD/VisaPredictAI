"""C5: el kit común de figuras y lo que garantiza sobre los tres generadores.

Lo que estas pruebas fijan es que el idioma y el tema dejaron de ser estado global:
viajan como argumento, son inmutables, y los rcParams vuelven a su sitio pase lo que
pase. Antes una pasada EN/oscura podía teñir la siguiente ES/clara dentro del mismo
proceso, y los reportes tenían que llamar a funciones privadas para «poner» el idioma.
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pytest  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
GENERATORS = (
    "experiments/make_gallery_figures.py",
    "experiments/make_fe_figures.py",
    "experiments/make_result_figures.py",
)
REPORTS = ("experiments/build_eda_report.py", "experiments/build_fe_report.py")


@pytest.fixture
def kit():
    import sys

    sys.path.insert(0, str(ROOT / "experiments"))
    import _figkit

    return _figkit


@pytest.fixture
def ctx(kit):
    colors = {
        "PAPER": "#ffffff",
        "INK": "#111111",
        "GRAY": "#666666",
        "MID": "#cccccc",
        "GRID": "#eeeeee",
        "BLUE": "#003ca6",
        "TEAL": "#008080",
        "WINE": "#7a1f3d",
    }
    return kit.FigureContext(
        # mismos overrides que la familia de galerías: font.size 9 y rejilla apagada
        theme=kit.Theme("light", colors, {"font.size": 9, "axes.grid": False}),
        lang=kit.LangCtx("es", {"footer": "{mes} {anio}"}, {1: "enero"}, {"mexico": "México"}),
    )


class TestImmutableState:
    def test_theme_and_lang_are_frozen(self, kit, ctx) -> None:
        for target, field, value in ((ctx.theme, "name", "dark"), (ctx.lang, "code", "en"), (ctx, "theme", None)):
            with pytest.raises(dataclasses.FrozenInstanceError):
                setattr(target, field, value)

    def test_the_context_mappings_cannot_be_mutated_in_place(self, kit, ctx) -> None:
        for mapping in (ctx.theme.colors, ctx.theme.overrides, ctx.lang.txt, ctx.lang.months, ctx.lang.cname):
            with pytest.raises(TypeError):
                mapping["INTRUSO"] = "x"  # type: ignore[index]

    def test_mutating_the_original_dict_does_not_change_the_context(self, kit) -> None:
        colors = {"PAPER": "#ffffff", "INK": "#000000"}
        textos = {"footer": "{mes} {anio}"}
        meses = {1: "enero"}
        paises = {"mexico": "México"}
        theme = kit.Theme("light", colors, {"font.size": 9})
        lang = kit.LangCtx("es", textos, meses, paises)
        colors["PAPER"] = "#000000"
        textos["footer"] = "roto"
        meses[1] = "roto"
        paises["mexico"] = "roto"
        assert theme.colors["PAPER"] == "#ffffff"
        assert lang.txt["footer"] == "{mes} {anio}"
        assert lang.months[1] == "enero"
        assert lang.cname["mexico"] == "México"

    @pytest.mark.parametrize("nombre", ["sepia", "Light", "", "claro"])
    def test_the_theme_domain_is_closed(self, kit, nombre: str) -> None:
        with pytest.raises(ValueError, match="tema desconocido"):
            kit.Theme(nombre, {"PAPER": "#fff"})

    @pytest.mark.parametrize("code", ["fr", "ES", "", "es-MX"])
    def test_the_language_domain_is_closed(self, kit, code: str) -> None:
        with pytest.raises(ValueError, match="idioma desconocido"):
            kit.LangCtx(code, {}, {})

    def test_building_a_context_does_not_touch_global_rcparams(self, kit) -> None:
        before = dict(plt.rcParams)
        kit.FigureContext(theme=kit.Theme("dark", {"PAPER": "#000"}), lang=kit.LangCtx("en", {}, {}))
        assert dict(plt.rcParams) == before

    def test_pick_does_not_mutate_the_palette_it_received(self, kit) -> None:
        colors = {"BLUE": "#00f", "INK": "#000"}
        theme = kit.Theme("light", colors)
        theme.pick("BLUE")
        assert colors == {"BLUE": "#00f", "INK": "#000"}

    def test_a_missing_colour_fails_loudly(self, kit) -> None:
        with pytest.raises(KeyError, match="NO_EXISTE"):
            kit.Theme("light", {"BLUE": "#00f"}).pick("BLUE", "NO_EXISTE")

    def test_asking_for_countries_where_there_are_none_is_an_error(self, kit) -> None:
        with pytest.raises(AttributeError, match="nombres de país"):
            _ = kit.LangCtx("es", {}, {}).cname


class TestVariants:
    def test_the_four_variants_run_in_a_fixed_order(self, kit) -> None:
        assert kit.VARIANTS == (("es", "light"), ("es", "dark"), ("en", "light"), ("en", "dark"))

    def test_each_variant_writes_to_its_own_subdirectory(self, kit) -> None:
        rutas = {}
        for lang, theme in kit.VARIANTS:
            c = kit.FigureContext(theme=kit.Theme(theme, {"PAPER": "#fff"}), lang=kit.LangCtx(lang, {}, {}))
            rutas[c.variant] = str(c.subdir)
        assert rutas == {"es/light": ".", "es/dark": "dark", "en/light": "en", "en/dark": "en/dark"}

    def test_only_the_spanish_light_variant_is_the_academic_one(self, kit) -> None:
        academicas = [
            f"{lang}/{theme}"
            for lang, theme in kit.VARIANTS
            if kit.FigureContext(theme=kit.Theme(theme, {"PAPER": "#fff"}), lang=kit.LangCtx(lang, {}, {})).academic
        ]
        assert academicas == ["es/light"]

    def test_every_maker_runs_once_per_variant_and_closes_its_figure(self, kit, ctx) -> None:
        vistos: list[str] = []

        def maker(c):
            vistos.append(c.variant)
            return plt.figure()

        abiertas = len(plt.get_fignums())
        kit.run_variants(
            [maker],
            lambda lang, theme: kit.FigureContext(
                theme=kit.Theme(theme, dict(ctx.theme.colors)), lang=kit.LangCtx(lang, {}, {})
            ),
        )
        assert vistos == ["es/light", "es/dark", "en/light", "en/dark"]
        assert len(plt.get_fignums()) == abiertas  # ninguna figura queda abierta

    def test_the_variant_set_can_be_narrowed_and_reordered(self, kit, ctx) -> None:
        """`variants` existe para que el llamador acote o reordene las pasadas.

        Es lo que permite comprobar que el resultado NO depende del orden: la misma figura
        sale igual en `es/light → en/dark` que al revés.
        """
        vistos: list[str] = []

        def maker(c):
            vistos.append(c.variant)
            return plt.figure()

        contexto = lambda lang, theme: kit.FigureContext(  # noqa: E731
            theme=kit.Theme(theme, dict(ctx.theme.colors)), lang=kit.LangCtx(lang, {}, {})
        )
        kit.run_variants([maker], contexto, variants=(("es", "light"), ("en", "dark")))
        assert vistos == ["es/light", "en/dark"]

        vistos.clear()
        kit.run_variants([maker], contexto, variants=(("en", "dark"), ("es", "light")))
        assert vistos == ["en/dark", "es/light"]
        assert len(plt.get_fignums()) == 0

    def test_a_failing_maker_propagates_and_leaves_no_tint(self, kit, ctx) -> None:
        before = dict(plt.rcParams)

        def revienta(c):
            raise RuntimeError("el maker falló")

        with pytest.raises(RuntimeError, match="el maker falló"):
            kit.run_variants([revienta], lambda lang, theme: ctx)
        assert dict(plt.rcParams) == before


class TestReversibleStyle:
    def test_the_style_applies_the_exact_values_and_then_restores_them(self, kit, ctx) -> None:
        # Discriminante: se fija ANTES un valor distinto del que el tema va a poner, se
        # comprueba el valor exacto DENTRO y la restauración exacta al salir.
        plt.rcParams["figure.facecolor"] = "#123456"
        plt.rcParams["font.size"] = 33.0
        before = dict(plt.rcParams)
        with kit.figure_style(ctx):
            assert plt.rcParams["figure.facecolor"] == ctx.theme.colors["PAPER"]
            assert plt.rcParams["font.size"] == 9  # override de familia
            assert plt.rcParams["font.family"] == ["serif"]  # BASE_RC
            assert plt.rcParams["axes.grid"] is False
        assert plt.rcParams["figure.facecolor"] == "#123456"
        assert plt.rcParams["font.size"] == 33.0
        assert dict(plt.rcParams) == before

    def test_rcparams_are_restored_when_the_body_raises(self, kit, ctx) -> None:
        before = dict(plt.rcParams)
        with pytest.raises(ValueError), kit.figure_style(ctx):
            raise ValueError("boom")
        assert dict(plt.rcParams) == before

    def test_two_contexts_can_be_interleaved_without_bleeding(self, kit, ctx) -> None:
        oscuro = kit.FigureContext(
            theme=kit.Theme("dark", {**dict(ctx.theme.colors), "PAPER": "#101010"}), lang=ctx.lang
        )
        with kit.figure_style(ctx):
            claro = plt.rcParams["figure.facecolor"]
            with kit.figure_style(oscuro):
                assert plt.rcParams["figure.facecolor"] != claro
            assert plt.rcParams["figure.facecolor"] == claro


class TestSaving:
    def test_the_academic_variant_emits_png_and_the_allowed_pdf(self, kit, ctx, tmp_path) -> None:
        png, pdf = tmp_path / "png", tmp_path / "pdf"
        pdf.mkdir()
        fig = plt.figure()
        kit.save_dual(fig, "g01_panel", ctx, png_root=png, pdf_root=pdf, pdf_name=lambda n: f"eda3_{n}", announce=False)
        assert (png / "g01_panel.png").exists()
        assert (pdf / "eda3_g01_panel.pdf").exists()
        plt.close(fig)

    def test_a_figure_outside_the_pdf_policy_only_gets_its_png(self, kit, ctx, tmp_path) -> None:
        png, pdf = tmp_path / "png", tmp_path / "pdf"
        pdf.mkdir()
        fig = plt.figure()
        kit.save_dual(fig, "g02", ctx, png_root=png, pdf_root=pdf, pdf_name=lambda n: None, announce=False)
        assert (png / "g02.png").exists()
        assert not any(pdf.iterdir())
        plt.close(fig)

    @pytest.mark.parametrize(
        ("lang", "theme", "sub"), [("es", "dark", "dark"), ("en", "light", "en"), ("en", "dark", "en/dark")]
    )
    def test_the_web_only_variants_emit_just_their_png(self, kit, ctx, tmp_path, lang, theme, sub) -> None:
        c = kit.FigureContext(theme=kit.Theme(theme, dict(ctx.theme.colors)), lang=kit.LangCtx(lang, {}, {}))
        png, pdf = tmp_path / "png", tmp_path / "pdf"
        pdf.mkdir()
        fig = plt.figure()
        kit.save_dual(fig, "g01", c, png_root=png, pdf_root=pdf, pdf_name=lambda n: f"eda3_{n}", announce=False)
        assert (png / sub / "g01.png").exists()
        assert not any(pdf.iterdir())  # el PDF es solo del entregable académico
        plt.close(fig)

    def test_the_figure_comes_back_alive_for_the_caller_to_close(self, kit, ctx, tmp_path) -> None:
        pdf = tmp_path / "pdf"
        pdf.mkdir()
        fig = plt.figure()
        out = kit.save_dual(fig, "g01", ctx, png_root=tmp_path / "png", pdf_root=pdf, pdf_name=None, announce=False)
        assert out is fig
        assert fig.number in plt.get_fignums()  # el kit NO la cierra: es del caller
        plt.close(fig)


class TestGeneratorsHaveNoGlobalState:
    @pytest.mark.parametrize("path", GENERATORS)
    def test_no_module_level_language_or_theme_globals(self, path: str) -> None:
        src = (ROOT / path).read_text()
        tree = ast.parse(src)
        asignados = {
            t.id for node in tree.body if isinstance(node, ast.Assign) for t in node.targets if isinstance(t, ast.Name)
        }
        assert "LANG" not in asignados, f"{path}: LANG sigue siendo un global de módulo"
        assert "DARK_MODE" not in asignados, f"{path}: DARK_MODE sigue siendo un global de módulo"

    @pytest.mark.parametrize("path", GENERATORS)
    def test_no_function_rebinds_module_state(self, path: str) -> None:
        tree = ast.parse((ROOT / path).read_text())
        globales = [n for n in ast.walk(tree) if isinstance(n, ast.Global)]
        assert not globales, f"{path}: {len(globales)} sentencia(s) `global` viva(s)"

    @pytest.mark.parametrize("path", GENERATORS + REPORTS)
    def test_the_private_language_and_theme_setters_are_gone(self, path: str) -> None:
        tree = ast.parse((ROOT / path).read_text())
        llamadas = {
            n.func.attr if isinstance(n.func, ast.Attribute) else n.func.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute | ast.Name)
        }
        assert "_apply_lang" not in llamadas, f"{path}: sigue llamando a _apply_lang"
        assert "_apply_theme" not in llamadas, f"{path}: sigue llamando a _apply_theme"

    @pytest.mark.parametrize("path", ("experiments/make_gallery_figures.py", "experiments/make_fe_figures.py"))
    def test_no_maker_hardcodes_a_variant_subdirectory(self, path: str) -> None:
        src = (ROOT / path).read_text()
        for literal in ('"en"', '"dark"'):
            # los subdirectorios los decide el contexto; un literal suelto seria un camino
            # escrito a mano que se desincronizaria del kit.
            assert f"sub / {literal}" not in src, f"{path}: ruta de variante escrita a mano"

    def test_result_figures_keeps_its_monolingual_pdf_contract(self) -> None:
        """Usa el contexto reversible del kit, pero sin adquirir variantes que nunca tuvo.

        Se comprueba sobre el AST, no sobre el texto: un docstring que MENCIONE `save_dual`
        no es una llamada a `save_dual`.
        """
        tree = ast.parse((ROOT / "experiments/make_result_figures.py").read_text())
        llamadas = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "run_variants" not in llamadas, "los resultados no tienen variantes de idioma ni tema"
        assert "save_dual" not in llamadas, "su guardado es monolingüe y solo PDF"
        assert "results_context" in llamadas, "debe construir su contexto de imprenta"
        assert "figure_style" in llamadas, "debe envolver la generación en ese contexto"
        assert "_emit" in llamadas, "sigue duplicando el par savefig/close"

    def test_result_figures_builds_exactly_one_spanish_light_context(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("mrf_c5", ROOT / "experiments/make_result_figures.py")
        assert spec is not None and spec.loader is not None
        import sys

        sys.path.insert(0, str(ROOT / "experiments"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        ctx = mod.results_context()
        assert ctx.variant == "es/light"
        assert ctx.academic is True
        assert ctx.theme.overrides["axes.grid"] is True  # rejilla de imprenta, no de web


class TestGovernedInventory:
    def test_the_kit_is_registered_with_its_real_consumers(self) -> None:
        import json

        inv = json.loads((ROOT / "docs/experiments_inventory.json").read_text())
        assert "_figkit.py" in inv, "el kit no está en el inventario gobernado"
        consumidor = inv["_figkit.py"]["consumidor"]
        for consumer in (
            "make_gallery_figures",
            "make_fe_figures",
            "make_result_figures",
            "build_eda_report",
            "build_fe_report",
        ):
            assert consumer in consumidor

    def test_only_the_doc_key_is_treated_as_metadata(self) -> None:
        src = (ROOT / "tools/check_experiments_inventory.py").read_text()
        assert 'k != "_doc"' in src
        # el filtro viejo descartaba TODO lo que empezara por "_", así que un módulo
        # interno no se podía registrar ni aunque se añadiera al JSON.
        assert 'not k.startswith("_")' not in src

    def test_an_unregistered_private_module_still_breaks_the_gate(self, tmp_path, monkeypatch) -> None:
        import importlib.util
        import json

        spec = importlib.util.spec_from_file_location("cei_c5", ROOT / "tools/check_experiments_inventory.py")
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        exp = tmp_path / "experiments"
        exp.mkdir()
        (exp / "_otro.py").write_text("x = 1\n")
        inv = tmp_path / "inv.json"
        inv.write_text(json.dumps({"_doc": "meta"}))
        monkeypatch.setattr(mod, "ROOT", tmp_path)
        monkeypatch.setattr(mod, "INVENTORY", inv)
        assert mod.main() == 1  # un privado sin registrar rompe

        inv.write_text(json.dumps({"_doc": "meta", "_otro.py": {"clase": "producto", "consumidor": "x"}}))
        assert mod.main() == 0  # registrado, pasa


class TestReportsCallTheirMakersCorrectly:
    """Lo que habría cazado el `TypeError` de `g06_pulso_fiscal(df, gfacts)` sin `ctx`.

    No basta buscar texto: se enlazan las once llamadas reales del reporte contra la
    firma viva de cada maker con `inspect.signature.bind`, que falla exactamente igual
    que fallaría la llamada.
    """

    def _module(self, path: str):
        import importlib.util
        import sys

        sys.path.insert(0, str(ROOT / "experiments"))
        spec = importlib.util.spec_from_file_location(f"c5_{pathlib.Path(path).stem}", ROOT / path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _maker_calls(self, path: str, alias: str, prefix: str) -> list[ast.Call]:
        tree = ast.parse((ROOT / path).read_text())
        return [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == alias
            and n.func.attr.startswith(prefix)
            and n.func.attr[1:2].isdigit()
        ]

    def test_the_eda_report_binds_its_eleven_maker_calls(self) -> None:
        import inspect

        gallery = self._module("experiments/make_gallery_figures.py")
        llamadas = self._maker_calls("experiments/build_eda_report.py", "gallery", "g")
        assert len(llamadas) == 11, f"se esperaban 11 llamadas a makers, hay {len(llamadas)}"
        for call in llamadas:
            assert isinstance(call.func, ast.Attribute)
            maker = getattr(gallery, call.func.attr)
            args = [a.id if isinstance(a, ast.Name) else "?" for a in call.args]
            # `bind` lanza TypeError con exactamente el mismo criterio que la llamada real
            inspect.signature(maker).bind(*args)

    def test_the_fe_report_binds_its_maker_calls(self) -> None:
        import inspect

        fefig = self._module("experiments/make_fe_figures.py")
        src = (ROOT / "experiments/build_fe_report.py").read_text()
        assert "mk(df, gfacts, ctx)" in src, "el reporte FE llama sus makers con contexto"
        for maker in fefig.MAKERS:
            inspect.signature(maker).bind("df", "facts", "ctx")

    def test_every_gallery_maker_requires_a_context(self) -> None:
        import inspect

        gallery = self._module("experiments/make_gallery_figures.py")
        for maker in gallery.MAKERS_DF + gallery.MAKERS_FACTS:
            params = list(inspect.signature(maker).parameters)
            assert params[-1] == "ctx", f"{maker.__name__} no recibe contexto"

    def test_the_reports_never_reach_into_private_generator_attributes(self) -> None:
        for path in REPORTS:
            tree = ast.parse((ROOT / path).read_text())
            privados = {
                f"{n.value.id}.{n.attr}"
                for n in ast.walk(tree)
                if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name)
                and n.value.id in {"gallery", "fefig"}
                and n.attr.startswith("_")
            }
            assert not privados, f"{path} usa API privada: {sorted(privados)}"
