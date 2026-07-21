#!/usr/bin/env python
"""RC-2: preflight del venv deep PRIVADO — se EJECUTA con el intérprete del venv efímero (`$DEEP_ENV/bin/python`), NO con
el Python global del runner (cuyo `lib` del toolcache es 0777/0775 y hace fallar la identidad gobernada). Exige:

- `sys.prefix == $DEEP_ENV` (el stack deep se instaló y corre en el venv privado, no en el global);
- `site-packages` (purelib) bajo el prefijo;
- la cadena desde `site-packages` HASTA el prefijo sin symlinks ni escritura grupo/otros — exactamente la superficie que
  `governed_import_identity` descenderá al certificar los orígenes.

Fail-closed: exit != 0 con diagnóstico. Stdlib-only."""

from __future__ import annotations

import os
import stat
import sys
import sysconfig


def main() -> int:
    env = os.environ.get("DEEP_ENV")
    if not env:
        print("preflight: DEEP_ENV no está en el entorno")
        return 1
    if sys.prefix != env:
        print(f"preflight: sys.prefix {sys.prefix!r} != DEEP_ENV {env!r} (¿se usó el Python global del runner?)")
        return 1
    purelib = sysconfig.get_paths()["purelib"]
    if not purelib.startswith(sys.prefix + os.sep):
        print(f"preflight: purelib {purelib!r} no está bajo el prefijo {sys.prefix!r}")
        return 1
    d = purelib
    while True:
        st = os.lstat(d)
        if stat.S_ISLNK(st.st_mode):
            print(f"preflight: symlink en la cadena gobernada: {d}")
            return 1
        if st.st_mode & 0o022:
            print(f"preflight: {d} escribible por grupo/otros ({oct(stat.S_IMODE(st.st_mode))})")
            return 1
        if d == sys.prefix:
            break
        d = os.path.dirname(d)
    print(f"preflight OK: venv deep privado gobernable en {sys.prefix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
