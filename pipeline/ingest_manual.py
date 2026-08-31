"""Validated manual ingestion of one bulletin page (A2: the human path while
travel.state.gov sits behind Cloudflare).

    ante/bin/python -m pipeline.ingest_manual <file.html> [--month YYYY-MM]

Everything is validated BEFORE the file can touch ``data/snapshots/`` (whose
master copy is S3, the source of truth): the filename -- or ``--month`` -- must
map to a bulletin month, the content must carry the bulletin markers, and BOTH
panel sections must parse covering the four block×table combinations with
enough rows -- the same completeness floor the ingestion gate enforces after a
rebuild (``REQUIRED_COMBOS`` / ``MIN_ROWS_NEW_MONTH``, single-sourced in
``vp_data.config``). A snapshot already frozen is IMMUTABLE: a byte-identical
re-ingest is a no-op (idempotent chain), a conflicting one aborts with no
escape hatch — exceptional replacement is out of scope for this path. The copy
is atomic (tmp + ``os.replace``).

Deliberately out of scope: pre-Oct-2015 layouts (no DFF table) fail the floor
-- archival recoveries go straight into ``data/snapshots/`` as always. No S3
upload and no rebuild happen here; ``make ingest-manual`` chains those.

stdout contract: the LAST line is the destination path (``make ingest-manual``
captures it for the create-only S3 put).
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

from pipeline.scrape_family_visa_bulletins import is_family_section
from pipeline.scrape_visa_bulletins import is_employment_section
from vp_data import config
from vp_data.visa_common import MONTH_MAP, extract_datetime_from_link, looks_like_bulletin, parse_tables

logger = logging.getLogger(__name__)
_MONTH_NAME = {number: name for name, number in MONTH_MAP.items()}


def _resolve_month(path: Path, month_flag: str | None) -> datetime:
    named = extract_datetime_from_link(path.name)
    if month_flag is None:
        if named is None:
            raise SystemExit(
                f"ERROR: '{path.name}' no mapea a un mes de boletín — pasa --month YYYY-MM "
                "(el destino se nombra visa-bulletin-for-<mes>-<año>.html)"
            )
        return named
    try:
        flagged = datetime.strptime(month_flag, "%Y-%m")
    except ValueError:
        raise SystemExit(f"ERROR: --month debe ser YYYY-MM (recibí {month_flag!r})") from None
    if named is not None and named != flagged:
        raise SystemExit(f"ERROR: --month {month_flag} no coincide con el mes del nombre del archivo ({named:%Y-%m})")
    return flagged


def _completeness_problems(soup: BeautifulSoup, month: datetime) -> list[str]:
    """The K3 floor, applied BEFORE the page can poison snapshots: both panel
    sections must parse, covering the four block×table combos with enough rows.
    Rows are estimated long-panel style: categories × country columns (the wide
    frame minus the category column and the two parser-added ones)."""
    combos: set[tuple[str, str]] = set()
    est_rows = 0
    for block, matcher in (("employment", is_employment_section), ("family", is_family_section)):
        for df in parse_tables(soup, month, matcher):
            if df.empty:
                continue
            combos.add((block, config.TABLE_MAP[str(df["table_type"].iloc[0])]))
            est_rows += len(df) * max(0, len(df.columns) - 3)
    problems = []
    missing = config.REQUIRED_COMBOS - combos
    if missing:
        problems.append(f"combinaciones bloque×tabla ausentes: {sorted(missing)}")
    if est_rows < config.MIN_ROWS_NEW_MONTH:
        problems.append(f"~{est_rows} filas estimadas (< {config.MIN_ROWS_NEW_MONTH})")
    return problems


def main(argv: list[str] | None = None) -> Path:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", type=Path, help="HTML del boletín descargado a mano")
    ap.add_argument("--month", help="mes del boletín (YYYY-MM) si el nombre del archivo no lo trae")
    args = ap.parse_args(argv)
    if not args.file.is_file():
        raise SystemExit(f"ERROR: no existe {args.file}")
    month = _resolve_month(args.file, args.month)
    content = args.file.read_bytes()
    if not looks_like_bulletin(content):
        raise SystemExit(f"ERROR: {args.file.name} no trae los marcadores de boletín (¿página WAF/incompleta?)")
    soup = BeautifulSoup(content.decode("utf-8", errors="replace"), "html.parser")
    problems = _completeness_problems(soup, month)
    if problems:
        raise SystemExit(
            "ERROR: el boletín no pasa el piso de completitud K3 — "
            + "; ".join(problems)
            + " (la vía manual exige boletines modernos completos; un mes de archivo va directo a data/snapshots/)"
        )
    dest = config.SNAPSHOTS_DIR / f"visa-bulletin-for-{_MONTH_NAME[month.month]}-{month.year}.html"
    if dest.exists():
        if dest.read_bytes() == content:
            # re-ingest byte-idéntico: no-op honesto, con el contrato de stdout
            # intacto (el put a S3 de la cadena es create-only y fallará si el
            # objeto ya existe — la copia maestra jamás se re-sube)
            logger.info("boletín %s ya congelado byte-idéntico (no-op)", dest.name)
            print(f"OK: {dest.name} ya estaba congelado byte-idéntico (no-op)")
            print(dest)
            return dest
        raise SystemExit(
            f"ERROR: {dest.name} ya está congelado con OTRO contenido — el snapshot es inmutable y esta vía "
            "no tiene escape (la sustitución excepcional queda fuera de alcance; resuélvela a mano con respaldo)"
        )
    config.SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(content)
    os.replace(tmp, dest)  # atomic: never a truncated snapshot on disk
    logger.info("boletín %s validado (4/4 bloque×tabla) e ingerido en %s", dest.name, config.SNAPSHOTS_DIR)
    print(f"OK: {month:%Y-%m} validado (4/4 bloque×tabla) → {dest.name}")
    print(dest)  # ÚLTIMA línea de stdout = destino (contrato de make ingest-manual)
    return dest


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    main()
