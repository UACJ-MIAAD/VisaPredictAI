#!/usr/bin/env python
"""B284/B290: instalador GOBERNADO de PyYAML para los gates YAML de CI (reemplaza el `pip install pyyaml==6.0.3` SIN
hashes de los jobs `consistency` y `p0r5-governance`).

Lee por `GovernanceSnapshot` (una observación sellada: O_NOFOLLOW, modo/uid/nlink exactos) los bytes gobernados de
`pyproject.toml` (el PIN, de `[project.optional-dependencies].dev`) y del lock Linux (`locks/dev-linux-x86_64.txt`, los
hashes YA existentes) y deriva EXACTAMENTE `pyyaml==<pin>` con sus hashes — NO recibe versión/hashes del caller, NO
descarga hashes nuevos, NO edita el lock. Luego:

1. crea un venv efímero 0700 bajo `$RUNNER_TEMP` (fuera del checkout);
2. escribe un requirements temporal 0600 con `O_EXCL|O_NOFOLLOW` + fsync de fichero y directorio;
3. sanea el entorno (`PIP_*`/`PYTHON*`/`HOME`/índices/config Git) para la instalación;
4. instala con `--no-deps --require-hashes` usando el pip del venv;
5. verifica bajo `-I` (aislado) por `importlib.metadata` (nombre/versión/RECORD) y exige `yaml.__spec__.origin` bajo
   `sys.prefix`, fichero regular, sin shadow local/zip/namespace/symlink;
6. emite un recibo JSON 0600 ligado a HEAD (observación git gobernada), sha del lock+pyproject, versión, origen y
   plataforma;
7. imprime la RUTA del venv en la última línea de stdout (para que el step de CI exporte `$GOV_ENV`).

Stdlib-only (+ `GovernanceSnapshot`). Fail-closed: cualquier desviación termina != 0. Su validador independiente
(`tools/validate_governance_bootstrap.py`) RE-observa el venv y el recibo."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import sys
import sysconfig
import tomllib

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(
        0, _ROOT
    )  # B284: raíz del repo en sys.path para importar `tools.governance_snapshot` en forma script
_PYPROJECT = "pyproject.toml"
_LOCK = "locks/dev-linux-x86_64.txt"
_DIST = "pyyaml"
_RECEIPT_SCHEMA = 1
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
# entorno saneado para la instalación: quita índices/config que pudieran redirigir la descarga o inyectar un pip.conf.
_ENV_DROP_PREFIXES = ("PIP_", "PYTHON", "UV_", "PDM_", "POETRY_")
_ENV_DROP_EXACT = ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "HOME", "GIT_CONFIG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_COUNT")  # fmt: skip


class _BootstrapError(RuntimeError):
    """Fallo fail-closed del bootstrap (mensaje humano)."""


def _pin_from_pyproject(data: bytes) -> str:
    """Deriva el pin EXACTO `pyyaml==X` de `[project.optional-dependencies].dev`. Fail-closed si falta/ambiguo."""
    try:
        doc = tomllib.loads(data.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise _BootstrapError(f"{_PYPROJECT}: no parseable ({exc})") from exc
    dev = (((doc.get("project") or {}).get("optional-dependencies") or {}).get("dev")) or []
    pins = [d for d in dev if isinstance(d, str) and d.lower().replace("-", "").startswith("pyyaml==")]
    if len(pins) != 1:
        raise _BootstrapError(f"{_PYPROJECT}: se esperaba EXACTAMENTE un pin pyyaml== en [dev], hay {len(pins)}")
    version = pins[0].split("==", 1)[1].strip()
    if not re.fullmatch(r"[0-9][0-9A-Za-z.\-]*", version):
        raise _BootstrapError(f"{_PYPROJECT}: versión pyyaml inválida {version!r}")
    return version


def _hashes_from_lock(data: bytes, version: str) -> list[str]:
    """Extrae los hashes sha256 del bloque `pyyaml==<version>` del lock (líneas continuadas con `\\`). Fail-closed."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _BootstrapError(f"{_LOCK}: no es UTF-8 ({exc})") from exc
    # reconstruye líneas lógicas (une continuaciones `\`)
    logical: list[str] = []
    buf = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            buf += line[:-1] + " "
        else:
            logical.append(buf + line)
            buf = ""
    if buf:
        logical.append(buf)
    target = f"{_DIST}=={version}"
    matches = [ln for ln in logical if ln.strip().lower().startswith(target)]
    if len(matches) != 1:
        raise _BootstrapError(f"{_LOCK}: se esperaba EXACTAMENTE un requerimiento {target}, hay {len(matches)}")
    hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", matches[0])
    if not hashes:
        raise _BootstrapError(f"{_LOCK}: {target} sin hashes sha256")
    if len(set(hashes)) != len(hashes):
        raise _BootstrapError(f"{_LOCK}: {target} con hashes duplicados")
    return sorted(hashes)


def _runner_temp() -> str:
    rt = os.environ.get("RUNNER_TEMP")
    if not rt:
        raise _BootstrapError("RUNNER_TEMP no está en el entorno (el bootstrap exige un temporal del runner 0700)")
    st = os.stat(rt)
    if not stat.S_ISDIR(st.st_mode):
        raise _BootstrapError(f"RUNNER_TEMP {rt!r} no es un directorio")
    rr, rrt = os.path.realpath(_ROOT), os.path.realpath(rt)
    if rrt == rr or rrt.startswith(rr + os.sep):  # ni el checkout mismo ni un subdirectorio de él
        raise _BootstrapError(f"RUNNER_TEMP {rt!r} está DENTRO del checkout (debe ser externo)")
    return rt


def _sanitized_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not (k.startswith(_ENV_DROP_PREFIXES) or k in _ENV_DROP_EXACT)}
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_INPUT"] = "1"
    env["PIP_NO_CACHE_DIR"] = "1"
    return env


def _write_requirements(path: str, version: str, hashes: list[str]) -> None:
    """Escribe el requirements 0600 con `O_EXCL|O_NOFOLLOW` + fsync de fichero y directorio (no puede pre-existir)."""
    body = f"{_DIST}=={version} " + " ".join(f"--hash=sha256:{h}" for h in hashes) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, body.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    dfd = os.open(os.path.dirname(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


_VERIFY_SRC = r"""
import importlib.metadata as m, json, os, stat, sys
d = "pyyaml"
info = {"version": m.version(d)}
files = m.files(d) or []
info["record_present"] = any(str(f).endswith("RECORD") for f in files)
import yaml
origin = yaml.__spec__.origin
info["origin"] = origin
prefix = sys.prefix + os.sep
ok = (
    isinstance(origin, str)
    and origin.startswith(prefix)
    and not os.path.islink(origin)
    and stat.S_ISREG(os.stat(origin).st_mode)
    and yaml.__spec__.submodule_search_locations is not None
)
info["origin_governable"] = bool(ok)
print(json.dumps(info))
"""


def _venv_python(venv: str) -> str:
    return os.path.join(venv, "bin", "python")


def _install_and_verify(venv: str, req_path: str, env: dict[str, str], version: str) -> dict:
    py = _venv_python(venv)
    # CAPTURA la salida de pip: STDOUT del instalador es SÓLO la ruta del venv (el paso de CI la captura con `$(...)`);
    # dejar que pip escriba a stdout la contaminaba ("Downloading pyyaml…") y rompía `echo GOV_ENV >> $GITHUB_ENV`.
    pip = subprocess.run([py, "-m", "pip", "install", "--no-deps", "--require-hashes", "-r", req_path], env=env, capture_output=True, text=True)  # fmt: skip
    if pip.returncode != 0:
        raise _BootstrapError(f"pip install falló (rc={pip.returncode}):\n{pip.stdout}\n{pip.stderr}")
    proc = subprocess.run([py, "-I", "-c", _VERIFY_SRC], check=True, capture_output=True, text=True, env=env)
    try:
        info = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise _BootstrapError(f"verificación: salida no-JSON ({exc}): {proc.stdout!r}") from exc
    if info.get("version") != version:
        raise _BootstrapError(f"verificación: versión instalada {info.get('version')!r} != pin {version!r}")
    if not info.get("record_present"):
        raise _BootstrapError("verificación: la distribución pyyaml no expone RECORD (metadata incompleta)")
    if not info.get("origin_governable"):
        raise _BootstrapError(f"verificación: yaml.__spec__.origin no gobernable ({info.get('origin')!r})")
    return info


def _head_commit() -> str | None:
    from tools.governance_snapshot import GovernanceSnapshot, GovernanceSnapshotError

    try:
        return GovernanceSnapshot(_ROOT).head_commit()
    except GovernanceSnapshotError:
        return None


def _emit_receipt(path: str, receipt: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def main() -> int:
    from tools.governance_snapshot import GovernanceSnapshot, GovernanceSnapshotError

    try:
        with GovernanceSnapshot(_ROOT) as snap:
            pyproject = snap.read(_PYPROJECT, category="source").data
            lock = snap.read(_LOCK, category="source").data
            snap.reverify()
        version = _pin_from_pyproject(pyproject)
        hashes = _hashes_from_lock(lock, version)
        rt = _runner_temp()
        gov_env = os.path.join(rt, f".gov-bootstrap-{os.getpid()}")
        if os.path.exists(gov_env):
            raise _BootstrapError(f"el venv objetivo {gov_env!r} ya existe")
        subprocess.run([sys.executable, "-m", "venv", gov_env], check=True)
        os.chmod(gov_env, 0o700)
        env = _sanitized_env()
        req_path = os.path.join(gov_env, "governance-bootstrap-requirements.txt")
        _write_requirements(req_path, version, hashes)
        info = _install_and_verify(gov_env, req_path, env, version)
        receipt = {
            "schema_version": _RECEIPT_SCHEMA,
            "distribution": _DIST,
            "version": version,
            "head_commit": _head_commit(),
            "lock": _LOCK,
            "lock_sha256": hashlib.sha256(lock).hexdigest(),
            "pyproject_sha256": hashlib.sha256(pyproject).hexdigest(),
            "n_hashes": len(hashes),
            "origin": info["origin"],
            "venv_prefix": gov_env,
            "platform": sysconfig.get_platform(),
            "python_version": platform.python_version(),
        }
        _emit_receipt(os.path.join(gov_env, "governance-bootstrap-receipt.json"), receipt)
    except (_BootstrapError, GovernanceSnapshotError, OSError) as exc:
        print(f"✗ bootstrap de gobernanza fail-closed: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"✗ bootstrap de gobernanza: subcomando falló ({exc}) (fail-closed)", file=sys.stderr)
        return 1
    print(f"✓ pyyaml=={version} instalado gobernado en {gov_env} ({len(hashes)} hashes, origen {info['origin']})", file=sys.stderr)  # fmt: skip
    print(gov_env)  # última línea de stdout = ruta del venv (para exportar $GOV_ENV)
    return 0


if __name__ == "__main__":
    sys.exit(main())
