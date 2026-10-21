"""Model-card protocol notice: declaration and live accreditation are distinct."""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml


def notice(
    root: Path,
    inputs: dict[str, bytes | None],
    panel: bytes | None,
    *,
    reconciliation_root: Path | None = None,
    parent_root: Path | None = None,
) -> str:
    """Accredit the same bytes render consumes, never an authorization dict supplied by a caller."""
    try:
        rules = yaml.safe_load((root / "tools/consistency_rules.yml").read_text())
        protocol = rules.get("retro_protocol") if isinstance(rules, dict) else None
    except OSError, yaml.YAMLError:
        protocol = None
    if (reconciliation_root is None) != (parent_root is None):
        raise ValueError("MODEL_CARD: reconciliation_root y parent_root deben darse juntos")
    if reconciliation_root is not None and protocol != "f2-causal":
        raise ValueError("MODEL_CARD: no se acredita una reconciliación sin protocolo f2-causal declarado")
    if protocol == "pre-F1":
        return (
            "> ⚠️ **Métricas retrospectivas provisionales:** derivadas con el protocolo previo a la\n"
            "> corrección causal del relleno (F1, 2026-07-12). No promover con estas cifras."
        )
    if protocol != "f2-causal":
        return "> **Protocolo retrospectivo no verificado:** no se afirma que estas métricas sean causales ni previas a F1."
    if reconciliation_root is None:
        return (
            "> **Protocolo retrospectivo declarado:** `f2-causal`, sin acreditación de la reconciliación\n"
            "> en esta emisión. La declaración por sí sola no acredita las cifras ni autoriza promoción."
        )

    from vp_model import reconciliation as rc
    from vp_model import vinculo_publicacion as vp

    raw = (reconciliation_root / rc.RUTA_REL).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    doc = rc.leer(reconciliation_root, sha256=digest)
    if doc["status"] != "derived":
        raise ValueError("MODEL_CARD: sólo una reconciliación derived acredita los artefactos")
    assert parent_root is not None  # paired roots, checked above
    ok, output = vp.verificar_reconciliacion(reconciliation_root, parent_root, repo_root=root, sha256=digest)
    if not ok:
        raise ValueError(f"MODEL_CARD: reconciliación no acreditada: {output[-500:]}")
    rc.leer(reconciliation_root, sha256=digest)  # the verifier must have checked this exact document
    expected = {
        **doc["inventory"],
        **doc["mother_layers"],
        **{path: sha for source in doc["sources"] for path, sha in source["artifacts"].items()},
        **(doc.get("outputs") or {}),
    }
    different = [
        name
        for name, body in inputs.items()
        if body is None or hashlib.sha256(body).hexdigest() != expected.get(f"reports/{name}")
    ]
    if panel is None or f"sha256:{hashlib.sha256(panel).hexdigest()}" != doc["panel_sha256"]:
        different.append("panel")
    if different:
        raise ValueError(f"MODEL_CARD: los bytes renderizados no son los acreditados: {different}")
    return (
        "> **Protocolo retrospectivo:** `f2-causal`. Los artefactos de esta tarjeta se acreditaron\n"
        f"> contra la reconciliación `{doc['reconciliation_id']}` de la campaña `{doc['parent']['campaign_id']}`.\n"
        "> Esa acreditación retrospectiva no autoriza promover un retador: sigue vigente el gate prospectivo."
    )
