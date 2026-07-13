#!/usr/bin/env python
"""Contrato de PUBLICABILIDAD del manifiesto de campana (fuente unica, fail-closed).

``experiments/sync_all.sh`` y sus tests comparten ESTA logica en vez de un ``grep``.
Publicar exige un manifiesto de campana que EXISTA, sea JSON valido y selle ``dirty``
como ``false`` BOOLEANO explicito. Fail-closed ante:

  * manifiesto ausente (actualmente lo esta y esta gitignored);
  * ilegible / vacio / malformado;
  * sin la clave ``dirty``;
  * ``dirty`` != ``false`` booleano ( ``true`` · ``"false"`` string · ``0`` · ``null`` ).

Motivo (auditoria 13-jul-2026 ronda 8): el ``grep '"dirty": *true'`` era FAIL-OPEN — no
cubria manifiesto ausente/vacio/malformado, ni ``{"dirty" : true}`` con espacios o saltos,
ni un cambio TOCTOU entre el chequeo y el ``dvc push``. Una campana CAMPAIGN_DIAGNOSTIC
(dirty=true) podia llegar a produccion. Este contrato lo cierra.

Uso:  python -m tools.campaign_manifest --assert-publishable reports/campaign/campaign_manifest.json
       (exit 0 = publicable · exit 7 = BLOQUEADO, con el motivo en stderr)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def publish_blocker(path: str | Path) -> str | None:
    """Motivo (str) por el que NO se puede publicar este manifiesto, o None si es publicable."""
    p = Path(path)
    if not p.exists():
        return f"falta el manifiesto de campana {p} (sin identidad sellada)"
    try:
        m = json.loads(p.read_text())
    except (json.JSONDecodeError, ValueError) as e:
        return f"manifiesto malformado ({type(e).__name__}: {e})"
    except OSError as e:
        return f"manifiesto ilegible ({type(e).__name__})"
    if not isinstance(m, dict):
        return "el manifiesto no es un objeto JSON"
    if "dirty" not in m:
        return "el manifiesto no sella la clave `dirty`"
    # `is not False` es DELIBERADO: rechaza true, "false" (string), 0 (== False pero no es
    # False), null. Solo el booleano JSON `false` autoriza publicar.
    if m["dirty"] is not False:
        return f"dirty={m['dirty']!r} (campana diagnostica) — re-lanza OFICIAL desde arbol limpio"
    return None


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--assert-publishable", metavar="MANIFEST", required=True)
    ns = ap.parse_args(argv[1:])
    blocker = publish_blocker(ns.assert_publishable)
    if blocker:
        print(f"PUBLISH BLOQUEADO: {blocker}", file=sys.stderr)
        return 7
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
