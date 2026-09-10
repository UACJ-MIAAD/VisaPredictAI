"""M65-R1 · El nombre visible de una cohorte es UNA autoridad, y las dos superficies obedecen.

En el entregable la Tabla 24 decía `DFF/inestable` y la Figura 53 `DFF/no_estable` para la
**misma** celda, dos páginas seguidas: cada superficie se construía la etiqueta por su
cuenta. El mapa vive ahora en `vp_model.stability` y aquí se comprueba por comportamiento
—rotulando la figura de verdad—, no leyendo el código.

La segunda mitad fija la colocación de la leyenda del margen: estaba anclada FUERA del área
de trazado y la línea del eje la atravesaba, algo que sólo se ve abriendo el PDF.
"""

from __future__ import annotations

import json
import pathlib
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pytest

from vp_model import stability

RAIZ = pathlib.Path(__file__).resolve().parents[1]
FACTS = RAIZ / "reports" / "governance" / "e5_facts.json"
TABLA = RAIZ / "reports" / "latex" / "cohortes.tex"


@pytest.fixture(scope="module")
def facts() -> dict:
    return json.loads(FACTS.read_text())


@pytest.fixture(scope="module")
def figmod():
    sys.path.insert(0, str(RAIZ / "experiments"))
    import make_cohort_figure

    return make_cohort_figure


def _features(retro_rate: float, worst: float) -> stability.PreSplitFeatures:
    return stability.PreSplitFeatures(
        retro_rate_pre=retro_rate,
        worst_retro_scaled_pre=worst,
        continuity_pre=1.0,
        pct_frozen_pre=0.0,
        median_step_pre=30.0,
        step_cv_pre=0.1,
        retro36_pre=0,
        n_pre=60,
    )


class TestNombreVisible:
    def test_la_cohorte_con_guion_bajo_se_presenta_sin_el(self) -> None:
        assert stability.nombre_visible("no_estable") == "inestable"

    def test_la_otra_cohorte_se_presenta_tal_cual(self) -> None:
        assert stability.nombre_visible("estable") == "estable"

    def test_la_celda_junta_tabla_y_cohorte(self) -> None:
        assert stability.etiqueta_celda("DFF", "no_estable") == "DFF/inestable"
        assert stability.etiqueta_celda("FAD", "estable") == "FAD/estable"

    @pytest.mark.parametrize("desconocida", ["inestable", "muy_estable", "", "NO_ESTABLE"])
    def test_una_cohorte_que_la_regla_no_produce_no_se_puede_nombrar(self, desconocida: str) -> None:
        """Falla cerrado: una tercera cohorte debe romper la surface, no colarse en crudo."""
        with pytest.raises(ValueError, match="cohorte desconocida"):
            stability.nombre_visible(desconocida)

    def test_todo_lo_que_produce_la_regla_es_nombrable(self) -> None:
        """Ata el dominio declarado a lo que `classify` devuelve de verdad, en sus dos ramas."""
        estable = stability.classify(_features(0.0, 0.0))
        inestable = stability.classify(_features(stability.RETRO_RATE_MAX + 0.01, 0.0))
        assert {estable, inestable} == set(stability.COHORTES)
        for cohorte in (estable, inestable):
            assert stability.nombre_visible(cohorte)


class TestLasDosSuperficiesCoinciden:
    def test_cada_fila_del_router_nombra_un_grupo_que_existe_en_el_JSON(self, facts: dict) -> None:
        """La figura rotula desde `router_rows`; la tabla agrupa por clave. Deben cuadrar."""
        for fila in facts["router_rows"]:
            celda = stability.etiqueta_celda(fila["table"], fila["cohort"])
            assert celda in facts, f"la figura rotularía «{celda}», que no es un grupo de la tabla"

    def test_la_tabla_publicada_usa_el_nombre_visible(self) -> None:
        tex = TABLA.read_text()
        assert "inestable" in tex
        assert "no\\_estable" not in tex and "no_estable" not in tex

    def test_las_etiquetas_de_la_figura_no_traen_el_identificador_crudo(self, facts: dict, figmod) -> None:
        etiquetas = [f"{stability.etiqueta_celda(f['table'], f['cohort'])}  h={f['h']}" for f in facts["router_rows"]]
        assert any("inestable" in e for e in etiquetas)
        assert not any("no_estable" in e for e in etiquetas)


class TestLaFiguraRenderizada:
    """Se dibuja de verdad y se miran los artistas: rótulos y posición de la leyenda."""

    @pytest.fixture
    def dibujo(self, facts: dict, figmod, tmp_path, monkeypatch):
        png, pdf = tmp_path / "png", tmp_path / "pdf"
        png.mkdir()
        pdf.mkdir()  # `save_dual` crea el subdirectorio del PNG, no la raíz del PDF
        monkeypatch.setattr(figmod, "PNG_ROOT", png)
        monkeypatch.setattr(figmod, "PDF_ROOT", pdf)
        ctx = figmod.context_for("es", "light")
        fig = figmod.fig_router_effect(facts, ctx)
        yield fig, fig.axes[0]
        plt.close(fig)

    def test_ninguna_barra_se_rotula_con_no_estable(self, dibujo) -> None:
        _, ax = dibujo
        textos = [t.get_text() for t in ax.get_yticklabels()]
        assert textos, "la figura no rotuló ninguna barra"
        assert not [t for t in textos if "no_estable" in t], textos
        assert [t for t in textos if "inestable" in t], textos

    def test_los_rotulos_salen_de_la_autoridad_compartida(self, dibujo, facts: dict) -> None:
        _, ax = dibujo
        esperados = [f"{stability.etiqueta_celda(f['table'], f['cohort'])}  h={f['h']}" for f in facts["router_rows"]]
        assert [t.get_text() for t in ax.get_yticklabels()] == esperados

    def test_la_leyenda_vive_en_la_banda_reservada_de_arriba(self, dibujo) -> None:
        """Antes se anclaba en `len(filas) - 0.2`, por debajo de la última barra y fuera del eje."""
        _, ax = dibujo
        leyenda = next(t for t in ax.texts if "margen material" in t.get_text())
        _, y = leyenda.get_position()
        alto, bajo = ax.get_ylim()  # eje invertido: primer valor = borde inferior
        assert y < 0, f"la leyenda debe ir en la banda reservada sobre la primera barra, no en y={y}"
        assert bajo < y < alto, f"la leyenda está en y={y}, fuera de los límites ({bajo}, {alto})"

    def test_la_leyenda_no_pisa_la_linea_del_eje(self, dibujo) -> None:
        """Prueba de verdad contra el defecto: su caja no puede tocar el borde inferior."""
        fig, ax = dibujo
        fig.canvas.draw()
        leyenda = next(t for t in ax.texts if "margen material" in t.get_text())
        caja = leyenda.get_window_extent(fig.canvas.get_renderer())
        eje = ax.get_window_extent()
        assert caja.y0 > eje.y0, "la leyenda toca o cruza la línea inferior del eje"
        assert caja.y1 < eje.y1, "la leyenda se sale por arriba del área de trazado"

    def test_la_leyenda_no_se_monta_sobre_ninguna_barra(self, dibujo) -> None:
        fig, ax = dibujo
        fig.canvas.draw()
        leyenda = next(t for t in ax.texts if "margen material" in t.get_text())
        caja = leyenda.get_window_extent(fig.canvas.get_renderer())
        for barra in ax.patches:
            assert not caja.overlaps(barra.get_window_extent()), "la leyenda se monta sobre una barra"
