"""Resolve web census bindings before applying the existing prose rules."""

from __future__ import annotations

import re


def census_values(facts: dict, eda: dict) -> dict:
    keys = ("n_obs", "n_obs_F", "n_months", "n_retro_events", "n_series_structural")
    if any(type(facts.get(key)) is not int or facts[key] < 0 for key in keys):
        raise ValueError("census: invalid integer facts")
    if not facts["n_obs"] or not facts["n_months"]:
        raise ValueError("census: empty panel")
    if any(facts[key] != eda.get("panel", {}).get(key) for key in ("n_obs", "n_obs_F", "n_months")):
        raise ValueError("census: inconsistent panel")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", facts.get("date_last", "")) or facts["date_last"] != eda.get(
        "panel", {}
    ).get("date_last"):
        raise ValueError("census: invalid vintage")
    groups = dict.fromkeys(("family_FAD", "family_DFF", "employment_FAD", "employment_DFF"), 0)
    seen = set()
    dff = 0
    for row in eda.get("series", []):
        group = f"{row['block']}_{row['table']}"
        identity = tuple(row[key] for key in ("country", "block", "category", "table"))
        count = row["n_total"]
        if group not in groups or identity in seen or type(count) is not int or not 0 <= count <= facts["n_months"]:
            raise ValueError("census: invalid series")
        seen.add(identity)
        groups[group] += count
        if row["table"] == "DFF":
            dff = max(dff, count)
    if len(seen) != facts["n_series_structural"] or sum(groups.values()) != facts["n_obs"]:
        raise ValueError("census: series census differs from panel")
    regime = eda["regime"]
    if (
        any(type(regime.get(key)) is not int or regime[key] < 0 for key in ("F", "C", "U", "UNK"))
        or sum(regime.values()) != facts["n_obs"]
        or regime["F"] != facts["n_obs_F"]
    ):
        raise ValueError("census: invalid regimes")
    return {
        **{key: facts[key] for key in ("n_obs", "n_obs_F", "n_months", "n_retro_events", "date_last")},
        "regime_C": regime["C"],
        "dff_months": dff,
        **groups,
    }


def render_census(source: str, values: dict) -> str:
    def replace(match: re.Match) -> str:
        key, comma = match.groups()
        if key not in values:
            raise ValueError(f"census: unknown binding {key}")
        value = values[key]
        if type(value) is int and value >= 0:
            return f"{value:,}" if comma else str(value)
        if (
            key == "date_last"
            and not comma
            and isinstance(value, str)
            and re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value)
        ):
            return value
        raise ValueError(f"census: invalid binding {key}")

    result = re.sub(r"\{\{census:([A-Za-z_]+)(:comma)?\}\}", replace, source)
    if "{{census:" in result:
        raise ValueError("census: malformed binding")
    return result
