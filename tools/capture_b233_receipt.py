#!/usr/bin/env python
"""B256/B261/B267: verificador/exportador del recibo de diagnóstico B233 histórico (schema v3).

NO EXISTE todavía una certificación viva. La certificación viva (schema 4: ejecutar el build gobernado y capturar
argv/intérprete/HEAD/árbol/stdout/stderr/`pip check`/inventario `importlib.metadata`) se implementará en la fase
R9-B233, junto con su validador schema-4, y sólo entonces podrá escribir el recibo canónico. Mientras R9 siga NO-GO,
este script se limita a:

- `--verify` (por defecto): valida el recibo canónico con `tools.validate_b233_receipt` y devuelve su código.
- `--export`: emite el recibo canónico A STDOUT, y SÓLO si pasa la validación. Los bytes provienen de UNA sola lectura
  GOBERNADA fd-bound (openat encadenado + snapshot, sin seguir symlinks) validada por el mismo paso — no se reabre por
  ruta. Ante cualquier problema (identidad/modo/nlink/JSON/schema/derivación) NO emite bytes y sale 1. NO escribe
  ficheros ni sigue symlinks; no promete atomicidad de fichero (no escribe ficheros).
- `--certify`: sale con código 2 y el mensaje «pendiente R9/B233»; JAMÁS escribe el recibo canónico ni inventa un
  schema 4 incompleto.

Nunca escribe la ruta canónica del recibo ni ninguna otra. El recibo se (re)genera únicamente por la vía R9-B233.
"""

from __future__ import annotations

import argparse
import sys

from tools import validate_b233_receipt as vr


def _export() -> int:
    """Emite el recibo canónico validado a STDOUT (bytes de la MISMA lectura gobernada). Sin fichero, sin symlink."""
    data, problems = vr.read_and_validate_canonical()
    if data is None:
        sys.stderr.write("no se exporta: el recibo canónico no validó\n")
        for p in problems:
            sys.stderr.write(f"  - {p}\n")
        return 1
    try:
        sys.stdout.buffer.write(data)
    except BrokenPipeError:  # el lector cerró la tubería; el recibo canónico NO se toca en ningún caso
        return 1
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Verificador/exportador del recibo B233 histórico (no editar a mano).")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--verify", action="store_true", help="valida el recibo canónico (por defecto)")
    grp.add_argument("--export", action="store_true", help="emite el recibo validado A STDOUT (no escribe ficheros)")
    grp.add_argument("--certify", action="store_true", help="NO disponible hasta R9/B233 (sale 2)")
    args = ap.parse_args(argv[1:])
    if args.certify:
        sys.stderr.write(
            "certificación viva NO disponible: pendiente R9/B233. El diagnóstico histórico (schema v3) no se re-certifica "
            "aquí; la certificación viva (schema 4 + su validador) se implementará en la fase R9-B233 y sólo entonces "
            "podrá escribir el recibo canónico. Este script nunca escribe la ruta canónica.\n"
        )
        return 2
    if args.export:
        return _export()
    return vr.main([vr.__name__])  # por defecto: verificar


if __name__ == "__main__":
    sys.exit(main(sys.argv))
