"""G7 · reconciliación nueva — `fe_facts` se re-deriva desde lo reconciliado y su stage declara lo que lee.

El paso `facts-repro` de CI (`dvc repro --force` de `key_facts eda_facts fe_facts`) destapó que el `fe_facts`
versionado no se reproducía desde el árbol del candidato: `experiments/build_fe_facts.py` lee los pools de la campaña
(`reports/campaign/campaign_pool_{FAD,DFF}_family.csv`) para la dificultad del campeón, la campaña los reescribió, y ni
`dvc.yaml` los declaraba como dependencias ni la reconciliación re-derivaba `fe_facts`: en recon4 quedó como
inventario, con la selección de antes de la campaña (1 rasgo; re-derivada, 0).

Aquí se exige: la reconciliación NO se cierra sin `fe_facts` (JSON y `.tex`) y su informe re-derivados, y el stage
`fe_facts` declara como dependencia cada archivo que su script lee.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import yaml

from tests.conftest import requiere
from tests.test_g7_reconciliacion import (  # noqa: F401 — fixtures
    _codigo_del_checkout,
    _ensamblado,
    _rederivar,
    cab,
    cli,
    m5,
    mundo,
    rc,
    rm,
)

ROOT = Path(__file__).resolve().parents[1]
FE = (
    "reports/fe/fe_facts.json",
    "reports/latex/fe_facts.tex",
    "reports/fe/fe_report.pdf",
    "reports/fe/en/fe_report.pdf",
)


def test_cerrar_exige_fe_facts_y_su_informe_rederivados(rm: dict, capsys) -> None:  # noqa: F811
    """RED: con `fe_facts` fuera de `SALIDAS`, una reconciliación sin re-derivarlo cerraba en `derived`."""
    _ensamblado(rm, capsys)
    _rederivar(rm["recon"])
    for rel in FE:
        (rm["recon"] / rel).unlink(missing_ok=True)
    capsys.readouterr()
    assert cli.main(["--root", str(rm["recon"]), "close", "--parent-root", str(rm["madre"])]) != 0
    salida = capsys.readouterr()
    for rel in FE:
        assert rel in salida.err, (rel, salida.err[-800:])


def test_control_con_fe_facts_rederivado_cierra(rm: dict, capsys) -> None:  # noqa: F811
    _ensamblado(rm, capsys)
    _rederivar(rm["recon"])
    assert cli.main(["--root", str(rm["recon"]), "close", "--parent-root", str(rm["madre"])]) == 0
    assert set(FE) <= set(rc.leer(rm["recon"])["outputs"])


def test_el_stage_fe_facts_declara_todo_lo_que_lee_su_script() -> None:
    """RED: `build_fe_facts` lee los pools de la campaña y `dvc.yaml` no los declaraba: `dvc status` y
    `dvc-lock-fresh` decían «al día» mientras la salida quedaba obsoleta."""
    requiere(modulos=("darts",))  # importar el productor arrastra vp_model.metrics → darts (perfil `model`)
    sys.path.insert(0, str(ROOT / "experiments"))
    import build_fe_facts as b

    deps = set(yaml.safe_load((ROOT / "dvc.yaml").read_text(encoding="utf-8"))["stages"]["fe_facts"]["deps"])
    leidos = {b.PANEL, *b.POOLS.values()}
    faltan = sorted(str(p.relative_to(ROOT)) for p in leidos if str(p.relative_to(ROOT)) not in deps)
    assert faltan == [], f"el stage fe_facts no declara lo que su script lee: {faltan}"


def test_los_pdf_del_tex_que_escribe_el_informe_fe_son_salidas_declaradas() -> None:
    """RED: `build_fe_report` re-dibuja las siete figuras `f0N_*` y `save_dual` escribe su PDF del `.tex` como
    `reports/latex/Figures/fe_<sufijo>.pdf`; sin declararlas, el cierre las vería como archivos aparecidos."""
    arbol = ast.parse((ROOT / "experiments" / "make_fe_figures.py").read_text(encoding="utf-8"))
    figuras = [n.name for n in arbol.body if isinstance(n, ast.FunctionDef) and re.fullmatch(r"f\d\d_\w+", n.name)]
    assert len(figuras) == 7, figuras
    faltan = [f for f in figuras if f"{rc.FIGURAS}fe_{f.split('_', 1)[1]}.pdf" not in rc.SALIDAS_FIGURA]
    assert faltan == [], f"figuras del informe FE sin declarar como salida: {faltan}"
