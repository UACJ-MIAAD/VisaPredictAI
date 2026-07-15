#!/usr/bin/env python
"""sha256 (hex) del contenido de un fichero — P0R.5 · R9.4/B66 (extraído del `-c` hashlib de sync_all.sh).
Solo stdlib; sin efectos secundarios.

    python -m tools.hash_file <ruta>
"""

from __future__ import annotations

import hashlib
import sys


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("uso: hash_file <ruta>", file=sys.stderr)
        return 2
    with open(argv[1], "rb") as fh:
        print(hashlib.sha256(fh.read()).hexdigest())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
