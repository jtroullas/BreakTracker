"""
pdf_parser.py — Extract associate names + shift times from schedule PDFs.

Expected line format: "First Last ShiftTime"
    e.g.  "Alan Zambrano 6a-3p"
          "Maria Garcia 7:00am-3:00pm"
          "James Wilson OFF"

Returns a list of (name, shift) tuples — shift is None if not found on the line.
"""

from __future__ import annotations

import re
from io import BytesIO

import pdfplumber

# ── Patterns ──────────────────────────────────────────────────────────────────

# Shift-time: "6a-3p", "6:30a-3p", "7am-3pm", "14:00-22:00"
_SHIFT_RE = re.compile(
    r"\d{1,2}(?::\d{2})?\s*(?:[ap]m?)?"  # start time
    r"\s*[-\u2013]\s*"                    # separator (dash or en-dash)
    r"\d{1,2}(?::\d{2})?\s*(?:[ap]m?)?", # end time
    re.IGNORECASE,
)

# "First Last" — two consecutive title-case words
_NAME_RE = re.compile(r"\b([A-Z][a-zA-Z'\-]+)\s+([A-Z][a-zA-Z'\-]+)\b")

# Words that look title-case but are never part of a person's name
_BLOCKED = {
    "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun",
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
    "Total", "Hours", "Weekly", "Store", "Page", "Date",
    "Week", "Schedule", "Employee", "Team", "Name",
    "Shift", "Dept", "Department", "Manager", "Lead",
    "Position", "Role", "Title", "Off", "Vac", "Holiday",
    "Open", "Close", "Start", "End", "Break", "Lunch",
}


# ── Core helper ───────────────────────────────────────────────────────────────

def _extract(text: str) -> tuple[str, str | None] | None:
    """
    Parse one line / cell and return (name, shift) or None.

    - Finds the shift-time pattern first, captures it as a string.
    - Looks for 'First Last' in the text before the shift.
    - Rejects all-caps words and known schedule terms.
    """
    text = text.strip()
    if not text:
        return None

    # Locate and capture the shift time
    shift_match = _SHIFT_RE.search(text)
    shift: str | None = shift_match.group(0).strip() if shift_match else None

    # Search for the name only in the portion before the shift
    search_area = text[: shift_match.start()].rstrip() if shift_match else text

    name_match = _NAME_RE.search(search_area)
    if not name_match:
        return None

    first, last = name_match.group(1), name_match.group(2)

    if first.isupper() or last.isupper():       # all-caps header junk
        return None
    if first in _BLOCKED or last in _BLOCKED:   # schedule/day words
        return None

    return f"{first} {last}", shift


# ── Page-level extraction ─────────────────────────────────────────────────────

def _from_tables(page) -> list[tuple[str, str | None]]:
    """Extract (name, shift) from structured table cells."""
    results: list[tuple[str, str | None]] = []
    for table in page.extract_tables():
        for row in table:
            if not row:
                continue
            for cell in row[:4]:
                if cell:
                    entry = _extract(str(cell))
                    if entry:
                        results.append(entry)
                        break
    return results


def _from_text(page) -> list[tuple[str, str | None]]:
    """Extract (name, shift) from raw text lines."""
    raw = page.extract_text() or ""
    results: list[tuple[str, str | None]] = []
    for line in raw.splitlines():
        entry = _extract(line)
        if entry:
            results.append(entry)
    return results


# ── Public API ────────────────────────────────────────────────────────────────

def extract_names(pdf_bytes: bytes) -> list[tuple[str, str | None]]:
    """
    Return a sorted, deduplicated list of (name, shift) tuples.
    Tables are tried first; raw text is the fallback.
    """
    seen:    set[str]                    = set()
    results: list[tuple[str, str | None]] = []

    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            candidates = _from_tables(page) or _from_text(page)
            for name, shift in candidates:
                key = name.lower()
                if key not in seen:
                    seen.add(key)
                    results.append((name, shift))

    return sorted(results, key=lambda x: x[0])


def extract_raw_text(pdf_bytes: bytes) -> str:
    """Dump all raw PDF text — used by the debug endpoint."""
    chunks: list[str] = []
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        for i, page in enumerate(pdf.pages):
            chunks.append(f"=== Page {i + 1} ===\n{page.extract_text() or ''}")
    return "\n\n".join(chunks)
