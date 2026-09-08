"""F3 · el guardián deja de mirar solo lo que alguien cableó a mano.

Auditoría de partida (comprobada aquí): de las seis superficies, `key_facts.tex` estaba vigilada
por un bloque cableado exclusivamente para ella; `fe_facts.tex`, la tabla de horizonte, la
propuesta entregada y la documentación de producto **no las vigilaba nadie**. Ninguna regla de
texto entra en un archivo generado, así que una macro desalineada o una fila regenerada a medias
pasaban enteras.

Ahora los contratos se declaran (`tex_json`) y se validan igual: cada macro y cada fila del .tex
tiene autoridad en el JSON, y cada autoridad tiene su macro o su fila.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_consistency as cc  # noqa: E402

RULES = yaml.safe_load((ROOT / "tools" / "consistency_rules.yml").read_text())
# Lectura defensiva a propósito: sin los contratos, cada prueba falla por su cuenta diciendo
# cuál falta. Un error de colección solo diría «KeyError: tex_json».
CONTRACTS = {Path(c["tex"]).name: c for c in RULES.get("tex_json", [])}
VACIO: dict = {
    "kind": "macros",
    "tex": "",
    "json": "",
    "prefix": "",
    "authority": "explicit",
    "map": {},
    "generator": "",
}
MACROS = CONTRACTS.get("key_facts.tex", VACIO)
FE = CONTRACTS.get("fe_facts.tex", VACIO)
TABLE = CONTRACTS.get(
    "horizon_champion.tex", {**VACIO, "kind": "table", "columns": [], "groups": [], "rows_from": "{group}.x"}
)


@pytest.fixture(autouse=True)
def _f3_existe() -> None:
    if not CONTRACTS:
        pytest.fail("F3 no está en el árbol: `tex_json` no existe en consistency_rules.yml")


def _tex(nombre: str) -> str:
    return (ROOT / CONTRACTS[nombre]["tex"]).read_text()


def _json(nombre: str) -> dict:
    return json.loads((ROOT / CONTRACTS[nombre]["json"]).read_text())


class TestTheSurfacesAreDeclared:
    @pytest.mark.parametrize("nombre", ["key_facts.tex", "fe_facts.tex", "horizon_champion.tex"])
    def test_each_generated_tex_has_a_contract(self, nombre: str) -> None:
        assert nombre in CONTRACTS
        assert (ROOT / CONTRACTS[nombre]["json"]).exists()

    def test_the_frozen_proposal_and_the_docs_are_groups(self) -> None:
        assert RULES["artifacts"].get("proposal") == ["reports/latex/AnteproyectoVisaPredictAI.tex"]
        assert RULES.get("frozen") == ["proposal"]
        docs = RULES["artifacts"].get("docs", [])
        assert len(docs) >= 10 and all(d.startswith("docs/") for d in docs)
        assert not any("*" in d for d in docs), "la documentación se declara nominalmente, no por glob"

    def test_every_declared_doc_exists(self) -> None:
        faltan = [d for d in RULES["artifacts"].get("docs", []) if not (ROOT / d).exists()]
        assert faltan == [], f"documentos declarados que no existen: {faltan}"

    def test_the_live_tree_satisfies_every_contract(self) -> None:
        assert cc._tex_json_violations(RULES) == []


class TestTheMacroContractIsClosed:
    def test_a_macro_with_no_authority_is_named(self) -> None:
        tex = _tex("key_facts.tex") + "\n\\newcommand{\\factInventado}{7}\n"
        problemas = cc._macros_contract(MACROS, tex, _json("key_facts.tex"))
        assert any("factInventado SIN autoridad" in p for p in problemas)

    def test_a_missing_macro_is_named_with_its_generator(self) -> None:
        tex = "\n".join(linea for linea in _tex("key_facts.tex").splitlines() if "factNObs}" not in linea)
        problemas = cc._macros_contract(MACROS, tex, _json("key_facts.tex"))
        assert any("falta la macro \\factNObs" in p and "build_key_facts.py" in p for p in problemas)

    def test_a_duplicated_macro_is_named(self) -> None:
        tex = _tex("key_facts.tex") + "\n\\newcommand{\\factNObs}{27911}\n"
        problemas = cc._macros_contract(MACROS, tex, _json("key_facts.tex"))
        assert any("macro duplicada \\factNObs" in p for p in problemas)

    def test_a_drifted_value_is_named_on_both_sides(self) -> None:
        tex = _tex("key_facts.tex").replace("{27911}", "{27912}", 1)
        problemas = cc._macros_contract(MACROS, tex, _json("key_facts.tex"))
        assert any("factNObs" in p and "27912" in p for p in problemas)

    def test_the_thousands_variant_shares_its_authority(self) -> None:
        # `27{,}911` es el mismo hecho que 27911: la coma tipográfica no puede leerse como deriva.
        problemas = cc._macros_contract(MACROS, _tex("key_facts.tex"), _json("key_facts.tex"))
        assert problemas == []

    def test_a_drifted_thousands_variant_is_caught(self) -> None:
        tex = _tex("key_facts.tex").replace("{27{,}911}", "{27{,}912}", 1)
        problemas = cc._macros_contract(MACROS, tex, _json("key_facts.tex"))
        assert any("factNObsFmt" in p for p in problemas)


class TestTheExplicitMapIsChecked:
    def test_the_fe_macros_resolve_through_their_declared_paths(self) -> None:
        assert cc._macros_contract(FE, _tex("fe_facts.tex"), _json("fe_facts.tex")) == []

    def test_a_path_that_does_not_exist_is_reported_as_invalid_authority(self) -> None:
        contrato = {**FE, "map": {**FE["map"], "SelIn": "feature_selection.no_existe"}}
        problemas = cc._macros_contract(contrato, _tex("fe_facts.tex"), _json("fe_facts.tex"))
        assert any("autoridad inválida" in p for p in problemas)

    def test_a_len_over_something_that_is_not_a_collection_is_rejected(self) -> None:
        contrato = {**FE, "map": {**FE["map"], "SelIn": "len:fe_version"}}
        problemas = cc._macros_contract(contrato, _tex("fe_facts.tex"), _json("fe_facts.tex"))
        assert any("autoridad inválida" in p and "len:" in p for p in problemas)

    @pytest.mark.parametrize(
        ("expr", "esperado"),
        [("fe_version", "2.0.0"), ("len:fe_decisions", 9), ("len:cleaning_decisions", 12)],
    )
    def test_the_resolver_reads_what_it_declares(self, expr: str, esperado) -> None:
        assert cc._authority_value(_json("fe_facts.tex"), expr) == esperado


class TestTheTableContractIsClosed:
    def test_the_live_table_matches_its_json(self) -> None:
        assert cc._table_contract(TABLE, _tex("horizon_champion.tex"), _json("horizon_champion.tex")) == []

    def test_the_sticky_first_column_carries_the_table_name(self) -> None:
        filas = cc._table_rows(_tex("horizon_champion.tex"), True)
        assert filas[0][0] == "FAD" and filas[1][0] == "FAD"
        assert any(f[0] == "DFF" for f in filas)

    def test_a_missing_row_is_named(self) -> None:
        tex = _tex("horizon_champion.tex").replace(" & 12 & 0.722 & 1.021 & +29.3 & $\\checkmark$ \\\\\n", "", 1)
        problemas = cc._table_contract(TABLE, tex, _json("horizon_champion.tex"))
        assert any("falta la fila ('FAD', '12')" in p for p in problemas)

    def test_a_row_with_no_authority_is_named(self) -> None:
        tex = _tex("horizon_champion.tex").replace(
            "\\bottomrule", " & 99 & 1.0 & 1.0 & +0.0 & -- \\\\\n\\bottomrule", 1
        )
        problemas = cc._table_contract(TABLE, tex, _json("horizon_champion.tex"))
        assert any("SIN autoridad" in p and "99" in p for p in problemas)

    def test_a_duplicated_row_is_named(self) -> None:
        tex = _tex("horizon_champion.tex").replace(
            "FAD & 1 & 0.126 & 0.146 & +13.8 & $\\checkmark$ \\\\",
            "FAD & 1 & 0.126 & 0.146 & +13.8 & $\\checkmark$ \\\\\nFAD & 1 & 0.126 & 0.146 & +13.8 & $\\checkmark$ \\\\",
            1,
        )
        problemas = cc._table_contract(TABLE, tex, _json("horizon_champion.tex"))
        assert any("fila duplicada" in p for p in problemas)

    def test_a_row_with_the_wrong_number_of_cells_is_named(self) -> None:
        tex = _tex("horizon_champion.tex").replace(
            "FAD & 1 & 0.126 & 0.146 & +13.8 & $\\checkmark$ \\\\", "FAD & 1 & 0.126 & 0.146 & +13.8 \\\\", 1
        )
        problemas = cc._table_contract(TABLE, tex, _json("horizon_champion.tex"))
        assert any("5 celdas" in p for p in problemas)

    def test_a_drifted_number_is_named(self) -> None:
        tex = _tex("horizon_champion.tex").replace("& 0.126 &", "& 0.127 &", 1)
        problemas = cc._table_contract(TABLE, tex, _json("horizon_champion.tex"))
        assert any("drift" in p and "0.127" in p for p in problemas)

    def test_a_flipped_significance_mark_is_named(self) -> None:
        tex = _tex("horizon_champion.tex").replace(
            "FAD & 1 & 0.126 & 0.146 & +13.8 & $\\checkmark$", "FAD & 1 & 0.126 & 0.146 & +13.8 & --", 1
        )
        problemas = cc._table_contract(TABLE, tex, _json("horizon_champion.tex"))
        assert any("sig=" in p for p in problemas)


class TestTheFrozenProposalIsNotRewritten:
    def test_a_required_rule_over_a_frozen_group_is_a_contract_failure(self, tmp_path, monkeypatch, capsys) -> None:
        """Se siembra la regla prohibida y se corre el guardián de verdad: debe salir en rojo
        señalando el CONTRATO, no el texto de un documento que nadie va a reescribir."""
        reglas = {**RULES, "required": [{"fact": "n_obs", "forms": ["{n_obs}"], "in": ["proposal"]}]}
        sembrado = tmp_path / "rules.yml"
        sembrado.write_text(yaml.safe_dump(reglas, allow_unicode=True))
        monkeypatch.setattr(cc, "RULES_PATH", sembrado)
        assert cc.main() == 1
        salida = capsys.readouterr().out
        assert "regla `required` sobre grupo(s) congelado(s) ['proposal']" in salida

    def test_the_same_rule_over_a_live_group_is_not_a_contract_failure(self) -> None:
        """Control benigno: la prohibición es del grupo congelado, no de `required` en general."""
        reglas = {**RULES, "required": [{"fact": "n_obs", "forms": ["{n_obs}"], "in": ["deliverable"]}]}
        congelados = set(reglas["frozen"])
        assert not congelados.intersection(reglas["required"][0]["in"])

    def test_the_proposal_is_only_watched_by_tripwires(self) -> None:
        for r in RULES.get("required", []):
            assert "proposal" not in r["in"], "la propuesta no puede tener obligaciones: está congelada"
        vigilada = [r for r in RULES["forbidden"] if "proposal" in r["in"]]
        assert vigilada, "la propuesta debe estar vigilada por al menos un tripwire"
        # Y la documentación de producto también deja de estar fuera del radar.
        assert [r for r in RULES["forbidden"] if "docs" in r["in"]]


class TestTheBenignCasesStaySilent:
    def test_a_contract_whose_files_exist_and_agree_reports_nothing(self) -> None:
        for contrato in RULES["tex_json"]:
            tex = (ROOT / contrato["tex"]).read_text()
            data = json.loads((ROOT / contrato["json"]).read_text())
            fn = cc._macros_contract if contrato["kind"] == "macros" else cc._table_contract
            assert fn(contrato, tex, data) == []

    def test_a_missing_file_breaks_the_contract_instead_of_passing_quietly(self) -> None:
        reglas = {"tex_json": [{**MACROS, "tex": "reports/latex/no_existe.tex"}]}
        problemas = cc._tex_json_violations(reglas)
        assert any("contrato roto" in p for p in problemas)
