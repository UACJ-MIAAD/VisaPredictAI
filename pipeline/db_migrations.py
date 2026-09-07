"""Cadena de migraciones del almacén (H1).

`schema.sql` es la línea base, aplicada como migración 001 y fijada byte a byte contra
`pipeline/migrations/001_*.sql`; cada cambio estructural posterior es un
`NNN_*.sql` numerado. La cadena se aplica en orden, cada archivo dentro de su propia
transacción, sobre una base temporal que solo sustituye a la viva si todo sale bien
(`os.replace`, atómico): una migración fallida deja el almacén anterior intacto.

Las versiones aplicadas quedan en `schema_version` con el sha256 de cada archivo, y un
checksum que no cuadre contra la base viva anterior aborta el build: la historia es
inmutable.

Este módulo no sabe nada de datos ni de gobernanza; solo de DDL y de su orden.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schema.sql"
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MIGRATION_NAME = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")
_DOLLAR_TAG = re.compile(r"\$[A-Za-z_][A-Za-z_0-9]*\$|\$\$")


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    path: Path
    sha256: str


def migrations(migrations_dir: Path | None = None) -> list[Migration]:
    """Discover the migration chain: ``NNN_descripcion.sql``, contiguous from 001.

    Anything unparseable, duplicated or with a gap aborts — a hole in the chain
    means an edited history, and history is immutable (checksums pin the rest).
    """
    mig_dir = migrations_dir if migrations_dir is not None else MIGRATIONS_DIR
    found: dict[int, Migration] = {}
    for fp in sorted(mig_dir.glob("*.sql")):
        m = _MIGRATION_NAME.match(fp.name)
        if not m:
            raise SystemExit(f"migración con nombre inválido: {fp.name} (esperado NNN_descripcion.sql)")
        version = int(m.group(1))
        if version in found:
            raise SystemExit(f"versión de migración duplicada: {version:03d}")
        found[version] = Migration(version, m.group(2), fp, hashlib.sha256(fp.read_bytes()).hexdigest())
    if not found:
        raise SystemExit(f"sin migraciones en {mig_dir}")
    chain = [found[v] for v in sorted(found)]
    if [m.version for m in chain] != list(range(1, len(chain) + 1)):
        raise SystemExit(f"cadena de migraciones no contigua: {sorted(found)} (esperado 1..{len(chain)})")
    return chain


def _split_sql(sql: str) -> list[str]:
    """Split a SQL script into statements, respecting strings and comments.

    Replaces the old naive comment-strip + ``split(';')`` (M6): a ``;`` or
    ``--`` inside a single-quoted literal, double-quoted identifier, dollar
    quote or (nested) block comment no longer corrupts statements. Comments are
    dropped; statements come back stripped and non-empty.
    """
    stmts: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)

    def _flush() -> None:
        stmt = "".join(buf).strip()
        if stmt:
            stmts.append(stmt)
        buf.clear()

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch in ("'", '"'):  # string literal / quoted identifier ('' and "" escape)
            quote, start = ch, i
            i += 1
            while i < n:
                if sql[i] == quote:
                    if i + 1 < n and sql[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            else:
                raise ValueError(f"literal sin cerrar ({quote}) en el SQL, posición {start}")
            buf.append(sql[start:i])
            continue
        if ch == "-" and nxt == "-":  # line comment
            while i < n and sql[i] != "\n":
                i += 1
            buf.append("\n")  # keep the break so tokens never glue together
            continue
        if ch == "/" and nxt == "*":  # block comment (Postgres-style nesting)
            depth, i = 1, i + 2
            while i < n and depth:
                if sql[i] == "/" and i + 1 < n and sql[i + 1] == "*":
                    depth, i = depth + 1, i + 2
                elif sql[i] == "*" and i + 1 < n and sql[i + 1] == "/":
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            if depth:
                raise ValueError("comentario de bloque sin cerrar en el SQL")
            buf.append(" ")
            continue
        if ch == "$":  # dollar-quoted string ($tag$ ... $tag$)
            m = _DOLLAR_TAG.match(sql, i)
            if m:
                tag = m.group()
                end = sql.find(tag, m.end())
                if end == -1:
                    raise ValueError(f"dollar-quote sin cerrar ({tag}) en el SQL")
                buf.append(sql[i : end + len(tag)])
                i = end + len(tag)
                continue
        if ch == ";":
            _flush()
            i += 1
            continue
        buf.append(ch)
        i += 1
    _flush()
    return stmts


def _statements(sql: str) -> Iterator[str]:
    """Yield the executable statements of a SQL script (string/comment-aware)."""
    yield from _split_sql(sql)


def _apply_migrations(con: duckdb.DuckDBPyConnection, migs: list[Migration]) -> None:
    """Apply the chain in order, each migration inside its own transaction, then
    record every applied version in schema_version with its file checksum.

    The rollback story is layered: a failing statement rolls back ITS migration,
    and because the whole build happens in a temp file that only replaces the
    live database via os.replace on success, the previous warehouse stays intact.
    """
    for mig in migs:
        con.execute("BEGIN TRANSACTION")
        try:
            for stmt in _split_sql(mig.path.read_text(encoding="utf-8")):
                con.execute(stmt)
            con.execute("COMMIT")
        except Exception:  # noqa: BLE001 -- amplio a propósito: ROLLBACK garantizado ante CUALQUIER fallo, luego re-raise
            con.execute("ROLLBACK")
            logger.error("migración %03d (%s) FALLÓ — almacén previo intacto", mig.version, mig.description)
            raise
    applied_at = datetime.now(UTC)  # bitácora: wall-clock permitido aquí, jamás en hechos
    for mig in migs:
        con.execute(
            "INSERT INTO schema_version (version, description, applied_at, checksum) VALUES (?, ?, ?, ?)",
            [mig.version, mig.description, applied_at, mig.sha256],
        )


def verify_migration_history(prev_db_path: str | Path, migs: list[Migration]) -> None:
    """Fail-closed gate: every migration recorded in the previous live database
    must still exist as a file with the SAME sha256. An edited or deleted
    applied migration aborts the build (exit != 0) before touching anything.

    Tolerant only to what must be tolerated: a pre-migration warehouse (old
    schema_version without checksum column) or an unreadable file skip the gate
    with a warning — there is no recorded history to defend yet.
    """
    recorded: dict[int, str] = {}
    try:
        con = duckdb.connect(str(prev_db_path), read_only=True)
    except duckdb.Error as exc:
        logger.warning("almacén previo ilegible (%s) — gate de checksums omitido", exc)
        return
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info('schema_version')").fetchall()}
        if "checksum" not in cols:
            logger.info("almacén previo pre-migraciones (schema_version sin checksum) — gate omitido")
            return
        recorded = dict(con.execute("SELECT version, checksum FROM schema_version").fetchall())
    except duckdb.Error as exc:
        logger.warning("schema_version ilegible en el almacén previo (%s) — gate omitido", exc)
        return
    finally:
        con.close()
    by_version = {m.version: m for m in migs}
    for version, checksum in sorted(recorded.items()):
        mig = by_version.get(int(version))
        if mig is None:
            raise SystemExit(
                f"la migración {int(version):03d} está aplicada en el almacén vivo pero su archivo "
                f"desapareció de {MIGRATIONS_DIR} — el historial de migraciones es inmutable"
            )
        if mig.sha256 != checksum:
            raise SystemExit(
                f"checksum de la migración {int(version):03d} NO coincide con el aplicado en el almacén "
                f"vivo ({mig.sha256[:12]}… != {str(checksum)[:12]}…) — una migración aplicada jamás se "
                "edita; crea una migración nueva"
            )


SCHEMA_VERSION = migrations()[-1].version
