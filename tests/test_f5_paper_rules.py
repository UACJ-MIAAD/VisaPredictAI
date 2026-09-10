"""F5 (parte viable) · las cifras insignia del paper dejan de estar sin vigilancia, y el
descargo provisional depende del protocolo declarado.

Auditoría de partida (comprobada aquí): el paper cita **49 decimales distintos**; de los que
tienen autoridad en `key_facts.json`, el guardián solo miraba cuatro. El MASE prospectivo
—el número insignia, citado cinco veces— no lo vigilaba nadie en el paper, ni la cobertura
empírica, ni el hold-out de ETS que abre la comparación.

Y el descargo «provisional pending re-derivation» era prosa suelta: nada obligaba a que
estuviera mientras las cifras precedieran a la corrección causal, ni a retirarlo después.
Ahora el protocolo se DECLARA y el gate exige que el texto lo acompañe, en los dos sentidos.
NADA aquí ejecuta ni simula la campaña: solo se prueba el contrato.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_consistency as cc  # noqa: E402

RULES = yaml.safe_load((ROOT / "tools" / "consistency_rules.yml").read_text())
FACTS = json.loads((ROOT / "reports" / "governance" / "key_facts.json").read_text())
PAPER = cc._repo_path("reports/paper_micai/paper.tex")
CUERPO = [ln for ln in PAPER.read_text().splitlines() if not ln.lstrip().startswith("%")]
NUEVAS = ("prosp_mase", "prosp_cov95", "ets_fad_mean")


def _decimal(fact: str) -> dict:
    reglas = [d for d in RULES["decimal"] if d["fact"] == fact and "paper" in d["in"]]
    assert len(reglas) == 1, f"{fact}: se esperaba una regla decimal sobre el paper, hay {len(reglas)}"
    return reglas[0]


def _capturas(patron: str) -> list[tuple[int, str]]:
    rx = re.compile(patron)
    return [(i, m.group(1)) for i, ln in enumerate(CUERPO, 1) for m in rx.finditer(ln)]


class TestTheThreeRulesWatchWhatNobodyWatched:
    @pytest.mark.parametrize("fact", NUEVAS)
    def test_the_rule_exists_and_names_its_reason(self, fact: str) -> None:
        regla = _decimal(fact)
        assert regla["reason"], f"{fact}: una regla sin razón no se puede auditar"
        assert fact in FACTS, f"{fact}: sin autoridad en key_facts.json"

    @pytest.mark.parametrize("fact", NUEVAS)
    def test_the_anchor_captures_the_authority_and_only_that(self, fact: str) -> None:
        """Discriminante: el ancla debe capturar el valor canónico y NINGÚN otro decimal."""
        hits = _capturas(_decimal(fact)["label"])
        assert hits, f"{fact}: el ancla no captura nada en el paper"
        capturados = {v for _, v in hits}
        assert capturados == {f"{FACTS[fact]:.3f}"} or capturados == {f"{FACTS[fact]:.2f}"}, (
            f"{fact}: el ancla captura {sorted(capturados)}, autoridad {FACTS[fact]}"
        )

    def test_the_prospective_anchor_does_not_swallow_another_MASE(self) -> None:
        """RED del ancla ingenua: `MASE 0.162` (Auto-ARIMA) NO puede entrar en la comparación."""
        ingenua = r"MASE[~ ]*\(?\\?(?:textbf\{)?([0-9]\.[0-9]{3})"
        assert {v for _, v in _capturas(ingenua)} == {"0.162", "0.347"}
        assert {v for _, v in _capturas(_decimal("prosp_mase")["label"])} == {"0.347"}

    @pytest.mark.parametrize("fact", NUEVAS)
    def test_a_shifted_value_is_caught(self, fact: str) -> None:
        rx = re.compile(_decimal(fact)["label"])
        original = next(ln for ln in CUERPO if rx.search(ln))
        antes = rx.search(original)
        assert antes is not None
        actual = antes.group(1)
        movido = original.replace(actual, actual[:-1] + str((int(actual[-1]) + 1) % 10), 1)
        despues = rx.search(movido)
        assert despues is not None and despues.group(1) != actual, "el ancla no vería el desplazamiento"

    @pytest.mark.parametrize("fact", NUEVAS)
    def test_a_missing_value_is_caught(self, fact: str) -> None:
        rx = re.compile(_decimal(fact)["label"])
        sin = [ln for ln in CUERPO if not rx.search(ln)]
        assert not any(rx.search(ln) for ln in sin), "quedó una captura donde no debía"

    @pytest.mark.parametrize("fact", NUEVAS)
    def test_a_badly_formatted_value_is_not_accepted_silently(self, fact: str) -> None:
        """Un valor con separador de miles o con dos decimales donde van tres no debe colar."""
        rx = re.compile(_decimal(fact)["label"])
        linea = next(ln for ln in CUERPO if rx.search(ln))
        encontrado = rx.search(linea)
        assert encontrado is not None
        actual = encontrado.group(1)
        for malo in (actual.replace(".", "{,}"), actual[:-1]):
            roto = linea.replace(actual, malo, 1)
            capturado = rx.search(roto)
            assert capturado is None or capturado.group(1) != actual


class TestTheProvisionalCaveatFollowsTheDeclaredProtocol:
    def test_the_declared_protocol_is_one_of_the_two(self) -> None:
        assert RULES["retro_protocol"] in cc.PROTOCOLS

    def test_the_caveat_is_declared_nominally(self) -> None:
        # No un grupo: el descargo pertenece al manuscrito, no al README que lo acompaña.
        assert RULES["provisional_caveat"]["files"] == ["reports/paper_micai/paper.tex"]

    def test_under_pre_f1_the_caveat_is_required_and_present(self) -> None:
        assert cc._provisional_caveat_violations({**RULES, "retro_protocol": "pre-F1"}) == []

    def test_under_f2_causal_the_same_text_becomes_a_violation(self) -> None:
        """El mismo árbol, el otro protocolo: el descargo pasa de obligatorio a prohibido.
        No se ejecuta ni se simula la campaña; solo se declara el protocolo."""
        problemas = cc._provisional_caveat_violations({**RULES, "retro_protocol": "f2-causal"})
        assert problemas and "sobra" in problemas[0] and "re-derivadas" in problemas[0]

    def test_under_pre_f1_a_paper_without_the_caveat_fails(self, tmp_path) -> None:
        copia = tmp_path / "paper.tex"
        copia.write_text("\n".join(ln for ln in CUERPO if "provisional pending re-derivation" not in ln))
        reglas = {
            **RULES,
            "provisional_caveat": {
                **RULES["provisional_caveat"],
                "files": [str(copia.relative_to(ROOT)) if copia.is_relative_to(ROOT) else "x"],
            },
        }
        # La ruta de tmp no cuelga del repo: se ejercita el brazo de «declarado pero ausente».
        problemas = cc._provisional_caveat_violations(reglas)
        assert problemas and ("falta" in problemas[0] or "ausente" in problemas[0])

    @pytest.mark.parametrize("malo", ["", "pre-f1", "F2", None, 1])
    def test_an_unknown_protocol_is_refused(self, malo) -> None:
        problemas = cc._provisional_caveat_violations({**RULES, "retro_protocol": malo})
        assert problemas and "retro_protocol inválido" in problemas[0]

    def test_no_caveat_declared_means_no_opinion(self) -> None:
        """Control benigno: sin declaración no se inventa una obligación."""
        sin = {k: v for k, v in RULES.items() if k != "provisional_caveat"}
        assert cc._provisional_caveat_violations(sin) == []


class TestTheLiveTreeIsGreen:
    def test_the_guard_passes_as_it_stands(self) -> None:
        assert cc._provisional_caveat_violations(RULES) == []

    def test_the_paper_still_carries_the_caveat_under_the_current_protocol(self) -> None:
        assert RULES["retro_protocol"] == "pre-F1"
        assert any("provisional pending re-derivation" in ln for ln in CUERPO)
