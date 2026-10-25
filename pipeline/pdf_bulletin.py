"""Strict, offline extraction of modern five-country Visa Bulletin PDF matrices.

The PDF supplies every cell. No OCR guesses or transcribed month-specific values.
A layout change aborts before any historical data is rewritten.
"""

from __future__ import annotations

import html
import re
import subprocess
from datetime import datetime
from pathlib import Path

from vp_data.visa_common import MONTH_MAP

COUNTRIES = ("All Chargeability Areas Except Those Listed", "CHINA-mainland born", "INDIA", "MEXICO", "PHILIPPINES")
FAMILY = ("F1", "F2A", "F2B", "F3", "F4")
EMPLOYMENT = (
    "1st",
    "2nd",
    "3rd",
    "Other Workers",
    "4th",
    "Certain Religious Workers",
    "5th Unreserved",
    "5th Set Aside Rural",
    "5th Set Aside High Unemployment",
    "5th Set Aside Infrastructure",
)
_CELL = re.compile(r"(?<!\w)(?:\d{2}[A-Z]{3}\d{2}|C|U)(?!\w)")
_HEADING = re.compile(
    (
        r"[AB]\.\s+(FINAL ACTION DATES FOR (?:FAMILY-SPONSORED|EMPLOYMENT-BASED) PREFERENCE CASES|"
        r"DATES FOR FILING (?:FAMILY-SPONSORED VISA|OF EMPLOYMENT-BASED VISA) APPLICATIONS)"
    ).replace(" ", r"\s+")
)


def extract_text(pdf: Path) -> tuple[str, str]:
    """Poppler is explicit infrastructure; scanned or encrypted PDFs fail closed."""
    if pdf.suffix.lower() != ".pdf" or not pdf.read_bytes().startswith(b"%PDF-"):
        raise ValueError("se exige un PDF real, no HTML renombrado")
    try:
        proc = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, timeout=60, check=True)
        version = subprocess.run(["pdftotext", "-v"], capture_output=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"extracción PDF fallida (requiere Poppler pdftotext): {exc}") from exc
    text = proc.stdout.decode("utf-8", errors="strict")
    if "\ufffd" in text or not text.strip():
        raise ValueError("PDF sin texto inequívoco; no se aplica OCR ni se inventan celdas")
    return text, version.stderr.decode("utf-8").splitlines()[0]


def _validated_row(label: str, before: str, line: str, values: list[re.Match[str]]) -> list[str]:
    """Validate the complete token boundaries before assigning a canonical label."""
    compact = " ".join(re.sub(r"[^a-z0-9 ]", " ", before.lower()).split())
    required = label.lower()
    if label == "Certain Religious Workers":
        required = "certain religious"
    if label == "5th Set Aside High Unemployment":
        # The word Unemployment can wrap AFTER the value line.
        required = "5th set aside high"
    if label in (*FAMILY, "1st", "2nd", "3rd", "4th", "Other Workers") and compact != required:
        raise ValueError(f"etiqueta de fila no exacta: {label}: {compact[-120:]}")
    if label == "Certain Religious Workers" and compact not in {required, required + " workers"}:
        raise ValueError(f"etiqueta religiosa no exacta: {compact[-120:]}")
    if not re.search(r"(?<!\w)" + re.escape(required) + r"(?!\w)", compact):
        raise ValueError(f"etiqueta de fila no acreditada: {label}: {compact[-120:]}")
    if any(line[left.end() : right.start()].strip() for left, right in zip(values[:-1], values[1:], strict=True)):
        raise ValueError("texto no vacío entre celdas: extracción ambigua")
    if line[values[-1].end() :].strip():
        raise ValueError("texto después de las cinco celdas: formato ambiguo")
    tokens = [value.group() for value in values]
    for token in tokens:
        if token not in {"C", "U"}:
            datetime.strptime(token, "%d%b%y")
    return tokens


def parse_layout(text: str, month: str) -> list[dict]:
    """Return exactly 150 typed cells, preserving literal C and U statuses."""
    expected = datetime.strptime(month, "%Y-%m")
    titles = re.findall(r"Visa Bulletin For ([A-Za-z]+) (\d{4})", text, re.IGNORECASE)
    dates = {(int(year), MONTH_MAP.get(name.lower())) for name, year in titles}
    if dates != {(expected.year, expected.month)}:
        raise ValueError(f"mes del contenido no coincide con {month}: {sorted(dates)}")
    # Normalize wrapping only for headings; retain rows and column positions below.
    normalized = re.sub(r"-\s*\n\s*", "-", text)
    headings = list(_HEADING.finditer(normalized))
    if len(headings) != 4:
        raise ValueError(f"se requieren exactamente cuatro matrices; detectadas {len(headings)}")
    result = []
    combinations = set()
    for index, heading in enumerate(headings):
        title = heading.group(1)
        block = "family" if "FAMILY" in title else "employment"
        table = "FAD" if title.startswith("FINAL") else "DFF"
        if (block, table) in combinations:
            raise ValueError("matriz repetida o ambigua")
        combinations.add((block, table))
        end = headings[index + 1].start() if index + 1 < 4 else len(normalized)
        section = normalized[heading.end() : end]
        # The supported schema requires this exact country order; reordered
        # columns never silently inherit the old interpretation.
        header = re.search(r"All\s+CHINA-.*?Those Listed", section, re.DOTALL)
        if not header or not all(c in header.group() for c in ("INDIA", "MEXICO", "PHILIPPINES")):
            raise ValueError("cabecera de países ausente o distribución no soportada")
        positions = [header.group().find(c) for c in ("CHINA-", "INDIA", "MEXICO", "PHILIPPINES")]
        if positions != sorted(positions):
            raise ValueError("orden de países cambiado")
        labels = FAMILY if block == "family" else EMPLOYMENT
        rows: list[tuple[str, list[str]]] = []
        pending = ""
        for line in section[header.end() :].splitlines():
            values = list(_CELL.finditer(line))
            if len(values) == 5:
                before = pending + " " + line[: values[0].start()]
                if len(rows) >= len(labels):
                    raise ValueError("fila adicional de cinco celdas: extracción ambigua")
                label = labels[len(rows)]
                tokens = _validated_row(label, before, line, values)
                rows.append((label, tokens))
                pending = ""
            else:
                pending += " " + line.strip()
        if len(rows) != len(labels):
            raise ValueError(f"{block}/{table}: {len(rows)} filas; se exigen {len(labels)}")
        for label, row_values in rows:
            for country, value in zip(COUNTRIES, row_values, strict=True):
                result.append({"block": block, "table": table, "label": label, "country": country, "value": value})
    if len(result) != 150 or len(combinations) != 4:
        raise ValueError("inventario distinto de 150 celdas y cuatro matrices")
    return result


def as_html(cells: list[dict], month: str, pdf_sha256: str) -> bytes:
    name = next(name for name, number in MONTH_MAP.items() if number == int(month[5:]))
    parts = [
        f"<html><head><title>Visa Bulletin For {name.title()} {month[:4]}</title></head><body>",
        f'<meta name="pdf-sha256" content="{pdf_sha256}">',
    ]
    for block in ("family", "employment"):
        for table, heading in (("FAD", "FINAL ACTION DATES"), ("DFF", "DATES FOR FILING")):
            parts.append(
                f"<h2>{heading}</h2><table><tr><th>{block.title()}-based</th>"
                + "".join(f"<th>{html.escape(c)}</th>" for c in COUNTRIES)
                + "</tr>"
            )
            labels = FAMILY if block == "family" else EMPLOYMENT
            for label in labels:
                row = [c["value"] for c in cells if c["block"] == block and c["table"] == table and c["label"] == label]
                parts.append("<tr>" + "".join(f"<td>{html.escape(v)}</td>" for v in [label, *row]) + "</tr>")
            parts.append("</table>")
    parts.append("</body></html>\n")
    return "\n".join(parts).encode()
