"""Registro privado y serializado del publicador; nunca escribe dentro del repo."""

from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import subprocess
import uuid
from contextlib import ExitStack
from pathlib import Path

from tools import check_maintenance_release as check
from tools.maintenance_github import encoded


def _private(info: os.stat_result, *, directory: bool = False) -> None:
    mode = 0o700 if directory else 0o600
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or stat.S_IMODE(info.st_mode) != mode or info.st_uid != os.getuid():
        raise check.MaintenanceError("registro: propietario/tipo/permisos incorrectos")
    if not directory and info.st_nlink != 1:
        raise check.MaintenanceError("registro: enlace múltiple")


def _snapshot(info: os.stat_result) -> tuple:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_uid,
        info.st_mode,
        info.st_nlink,
    )


class Registry:
    def __init__(self, path: Path):
        self.path = path.absolute()
        self.fd = -1
        self.lock = -1
        self.stack = ExitStack()

    def __enter__(self):
        with ExitStack() as cleanup:
            self.fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            cleanup.callback(os.close, self.fd)
            _private(os.fstat(self.fd), directory=True)
            self.lock = os.open("registry.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
            cleanup.callback(os.close, self.lock)
            _private(os.fstat(self.lock))
            fcntl.flock(self.lock, fcntl.LOCK_EX)
            cleanup.callback(fcntl.flock, self.lock, fcntl.LOCK_UN)
            if _snapshot(os.fstat(self.lock)) != _snapshot(
                os.stat("registry.lock", dir_fd=self.fd, follow_symlinks=False)
            ):
                raise check.MaintenanceError("registro: lock sustituido")
            self.guard()
            self.stack = cleanup.pop_all()
            return self

    def __exit__(self, *_args):
        self.stack.close()

    def guard(self) -> None:
        current = os.stat(self.path, follow_symlinks=False)
        opened = os.fstat(self.fd)
        _private(current, directory=True)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise check.MaintenanceError("registro: directorio sustituido")

    def read(self, name: str) -> bytes | None:
        self.guard()
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.fd)
        except FileNotFoundError:
            return None
        try:
            before = os.fstat(fd)
            _private(before)
            chunks, total = [], 0
            while part := os.read(fd, 65536):
                chunks.append(part)
                total += len(part)
                if total > 1024 * 1024:
                    raise check.MaintenanceError("registro demasiado grande")
            after = os.fstat(fd)
            named = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            if _snapshot(before) != _snapshot(after) or _snapshot(after) != _snapshot(named):
                raise check.MaintenanceError("registro cambió durante lectura")
            return b"".join(chunks)
        finally:
            os.close(fd)

    def write(self, name: str, value: dict, expected: bytes | None) -> None:
        data = encoded(value)
        if len(data) > 1024 * 1024:
            raise check.MaintenanceError("registro demasiado grande para su contrato de lectura")
        if self.read(name) != expected:
            raise check.MaintenanceError("registro: CAS de bytes no coincide")
        temporary = ".pending-" + uuid.uuid4().hex
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        promoted = False
        try:
            offset = 0
            while offset < len(data):
                written = os.write(fd, data[offset:])
                if written <= 0:
                    raise OSError("escritura sin progreso")
                offset += written
            os.fsync(fd)
            self.guard()
            if self.read(name) != expected:
                raise check.MaintenanceError("registro cambió antes de promoción")
            os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            promoted = True
            os.fsync(self.fd)
        except OSError as exc:
            if promoted:
                raise check.MaintenanceError(
                    "promoción realizada; fsync del directorio falló: consultar status antes de reintentar"
                ) from exc
            raise
        finally:
            os.close(fd)
            try:
                os.unlink(temporary, dir_fd=self.fd)
            except FileNotFoundError:
                pass  # La promoción ya movió este staging privado.

    def identity(self) -> tuple[dict, str]:
        raw = self.read("identity.json")
        if raw is None:
            raise check.MaintenanceError("registro sin inicializar")
        value = check._json(raw)
        if set(value) != {"schema", "installation_id", "host", "uid", "root"}:
            raise check.MaintenanceError("identidad de registro inválida")
        if (value["schema"], value["host"], value["uid"], value["root"]) != (
            "metadata-maintenance-publisher/1",
            os.uname().nodename,
            os.getuid(),
            os.path.realpath(self.path),
        ):
            raise check.MaintenanceError("identidad copiada a otro host/UID/ruta")
        if (
            type(value["uid"]) is not int
            or not isinstance(value["installation_id"], str)
            or len(value["installation_id"]) != 32
        ):
            raise check.MaintenanceError("identidad de instalación inválida")
        return value, hashlib.sha256(raw).hexdigest()


def initialize(path: Path) -> dict:
    parent = subprocess.run(["git", "-C", str(path.parent), "rev-parse", "--is-inside-work-tree"], capture_output=True)
    if parent.returncode == 0:
        raise check.MaintenanceError("el registro debe estar fuera del árbol Git")
    path.mkdir(mode=0o700, exist_ok=True)
    with Registry(path) as registry:
        if registry.read("identity.json") is None:
            registry.write(
                "identity.json",
                {
                    "schema": "metadata-maintenance-publisher/1",
                    "installation_id": uuid.uuid4().hex,
                    "host": os.uname().nodename,
                    "uid": os.getuid(),
                    "root": os.path.realpath(path),
                },
                None,
            )
        value, publisher_id = registry.identity()
        return {**value, "publisher_id": publisher_id, "authorizes_publication": False}
