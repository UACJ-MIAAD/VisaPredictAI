#!/usr/bin/env python
"""B256/B261: verificador/exportador del recibo de diagnóstico B233 histórico (schema v3).

NO EXISTE todavía una certificación viva. La certificación viva (schema 4: ejecutar el build gobernado y capturar
argv/intérprete/HEAD/árbol/stdout/stderr/`pip check`/inventario `importlib.metadata`) se implementará en la fase
R9-B233, junto con su validador schema-4, y sólo entonces podrá escribir el recibo canónico. Mientras R9 siga NO-GO,
este script se limita a:

- `--verify` (por defecto): valida el recibo canónico con `tools.validate_b233_receipt` y devuelve su código.
- `--export`: imprime a stdout (o escribe en un fichero NUEVO create-only, sin seguir symlinks, atómico) el contenido
  del recibo histórico canónico, tal cual. No modifica el canónico.
- `--certify`: sale con código 2 y el mensaje «pendiente R9/B233»; JAMÁS escribe el recibo canónico ni inventa un
  schema 4 incompleto.

Nunca escribe la ruta canónica del recibo. El recibo se (re)genera únicamente por la vía R9-B233 documentada.
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CANONICAL = "reports/governance/b233_receipt.json"


def _export(dest: str | None) -> int:
    """Imprime el recibo canónico a stdout, o lo escribe en `dest` NUEVO (O_CREAT|O_EXCL|O_NOFOLLOW, atómico). Nunca
    sigue symlinks ni sobrescribe."""
    try:
        with open(os.path.join(ROOT, _CANONICAL), "rb") as fh:
            data = fh.read()
    except OSError as exc:
        sys.stderr.write(f"no se pudo leer el recibo canónico ({exc})\n")
        return 1
    if dest is None:
        sys.stdout.buffer.write(data)
        return 0
    try:
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        sys.stderr.write(f"{dest}: ya existe (export es create-only, no sobrescribe)\n")
        return 1
    except OSError as exc:
        sys.stderr.write(f"{dest}: no se pudo crear ({exc})\n")
        return 1
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except OSError as exc:
        sys.stderr.write(f"{dest}: escritura falló ({exc})\n")
        return 1
    print(f"✓ recibo exportado a {dest}")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Verificador/exportador del recibo B233 histórico (no editar a mano).")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--verify", action="store_true", help="valida el recibo canónico (por defecto)")
    grp.add_argument("--export", action="store_true", help="imprime/exporta el recibo histórico (no modifica el canónico)")  # fmt: skip
    grp.add_argument("--certify", action="store_true", help="NO disponible hasta R9/B233 (sale 2)")
    ap.add_argument("--out", default=None, help="con --export: fichero NUEVO create-only (sin symlink); por defecto stdout")  # fmt: skip
    args = ap.parse_args(argv[1:])
    if args.certify:
        sys.stderr.write(
            "certificación viva NO disponible: pendiente R9/B233. El diagnóstico histórico (schema v3) no se re-certifica "
            "aquí; la certificación viva (schema 4 + su validador) se implementará en la fase R9-B233 y sólo entonces "
            "podrá escribir el recibo canónico. Este script nunca escribe la ruta canónica.\n"
        )
        return 2
    if args.export:
        return _export(args.out)
    # por defecto: verificar
    from tools import validate_b233_receipt as vr

    return vr.main([vr.__name__])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
