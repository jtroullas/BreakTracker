"""
database.py — SQLite wiring for Break Tracker.
Keeps all DB concerns in one place (SRP FTW).
"""
import re
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "break_tracker.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS associates (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT    NOT NULL,
                department  TEXT    NOT NULL DEFAULT 'General',
                shift       TEXT,
                active      INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS breaks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                associate_id INTEGER NOT NULL REFERENCES associates(id),
                break_start  TEXT    NOT NULL,
                break_end    TEXT
            );
        """)
        # Safe migration: add shift column if it doesn't exist yet
        try:
            conn.execute("ALTER TABLE associates ADD COLUMN shift TEXT")
        except Exception:
            pass  # column already present

        # Safe migration: add notes column if it doesn't exist yet
        try:
            conn.execute("ALTER TABLE associates ADD COLUMN notes TEXT")
        except Exception:
            pass  # column already present

        # Safe migration: add called_out column if it doesn't exist yet
        try:
            conn.execute(
                "ALTER TABLE associates ADD COLUMN called_out INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass  # column already present

        # Singleton table for manager-only notes
        conn.execute("""
            CREATE TABLE IF NOT EXISTS management_notes (
                id         INTEGER PRIMARY KEY CHECK (id = 1),
                content    TEXT    NOT NULL DEFAULT '',
                pin        TEXT    NOT NULL DEFAULT '1891',
                updated_at TEXT
            )
        """)
        # Safe migration: add pin column if table existed before this version
        try:
            conn.execute("ALTER TABLE management_notes ADD COLUMN pin TEXT NOT NULL DEFAULT '1891'")
        except Exception:
            pass
        # Ensure the one-and-only row exists (after column is guaranteed present)
        conn.execute(
            "INSERT OR IGNORE INTO management_notes (id, content, pin) VALUES (1, '', '1891')"
        )

        # Safe migration: add break_type column to breaks if missing
        try:
            conn.execute(
                "ALTER TABLE breaks ADD COLUMN break_type TEXT NOT NULL DEFAULT 'break'"
            )
        except Exception:
            pass  # column already present

        # Seed sample associates if the table is empty
        count = conn.execute("SELECT COUNT(*) FROM associates").fetchone()[0]
        if count == 0:
            conn.executemany(
                "INSERT INTO associates (name) VALUES (?)",
                [("Maria Garcia",), ("James Wilson",), ("Ava Thompson",),
                 ("Liam Johnson",), ("Sophia Martinez",), ("Noah Davis",)],
            )


# ── Shift-time sort helper ─────────────────────────────────────────────────────────────────

_TIME_PARSE_RE = re.compile(
    r"(?P<hour>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<ampm>[ap]m?)",
    re.IGNORECASE,
)


def _shift_start_minutes(shift: str | None) -> int:
    """
    Parse the START time of a shift string into minutes since midnight.
    Associates with no shift sort to the end (returns 9999).

    Handles: "6am", "6:30am", "6am - 3pm", "1:30pm - 10pm", "14:00-22:00"
    """
    if not shift:
        return 9999

    # 24-hour format: "14:00" or "14:00-22:00"
    m24 = re.match(r"(\d{1,2}):(\d{2})", shift.strip())
    if m24 and not re.search(r"[ap]m", shift, re.I):
        return int(m24.group(1)) * 60 + int(m24.group(2))

    # 12-hour format: grab only the FIRST time token (start of shift)
    m = _TIME_PARSE_RE.search(shift)
    if not m:
        return 9999

    hour = int(m.group("hour"))
    mins = int(m.group("min") or 0)
    ampm = m.group("ampm").lower()

    if ampm.startswith("p") and hour != 12:
        hour += 12
    elif ampm.startswith("a") and hour == 12:
        hour = 0

    return hour * 60 + mins


def _shift_end_minutes(shift: str | None) -> int:
    """
    Parse the END time of a shift string into minutes since midnight.
    Returns 9999 when there's no shift or the end can't be determined.

    Grabs the LAST time token — the end of the shift.
    """
    if not shift:
        return 9999

    # 24-hour format: grab the last HH:MM pair
    if not re.search(r"[ap]m", shift, re.I):
        matches = re.findall(r"(\d{1,2}):(\d{2})", shift)
        if matches:
            h, m = matches[-1]
            return int(h) * 60 + int(m)
        return 9999

    # 12-hour format: grab the LAST match of the time pattern
    all_matches = list(_TIME_PARSE_RE.finditer(shift))
    if len(all_matches) < 2:
        return 9999  # need at least start + end

    m = all_matches[-1]
    hour = int(m.group("hour"))
    mins = int(m.group("min") or 0)
    ampm = m.group("ampm").lower()

    if ampm.startswith("p") and hour != 12:
        hour += 12
    elif ampm.startswith("a") and hour == 12:
        hour = 0

    return hour * 60 + mins


# ── Associates ───────────────────────────────────────────────────────────────────

def _enrich(rows: list[sqlite3.Row]) -> list[dict]:
    """
    Convert a list of associate Rows into dicts, then attach:
      - breaks_allowed    : max 15-min breaks per shift (0, 1, or 2)
      - breaks_taken_today: completed 'break' entries for today
      - breaks_remaining  : max(0, allowed - taken)
    """
    today = datetime.now().strftime("%Y-%m-%d")
    result = []
    with get_conn() as conn:
        for row in rows:
            d = dict(row)
            allowed = len(suggested_breaks(d["shift"]))
            taken = conn.execute(
                """
                SELECT COUNT(*) FROM breaks
                WHERE associate_id = ?
                  AND break_type   = 'break'
                  AND break_end    IS NOT NULL
                  AND DATE(break_start) = ?
                """,
                (d["id"], today),
            ).fetchone()[0]
            d["breaks_allowed"]     = allowed
            d["breaks_taken_today"] = taken
            d["breaks_remaining"]   = max(0, allowed - taken)
            result.append(d)
    return result


def get_all_associates() -> list[dict]:
    """Return all active associates sorted by shift start time, enriched with break counts."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT
                a.id,
                a.name,
                a.shift,
                a.notes,
                a.called_out,
                b.id           AS break_id,
                b.break_start  AS break_start,
                b.break_type   AS break_type
            FROM associates a
            LEFT JOIN breaks b
                   ON b.associate_id = a.id AND b.break_end IS NULL
            WHERE a.active = 1
        """).fetchall()
    enriched = _enrich(rows)
    return sorted(enriched, key=lambda r: _shift_start_minutes(r["shift"]))


def get_working_associates() -> list[dict]:
    """Associates actively working: not on break and not called out."""
    return [r for r in get_all_associates()
            if r["break_start"] is None and not r["called_out"]]


def get_on_break_associates() -> list[dict]:
    """Associates currently on break or lunch (not called out)."""
    on_break = [r for r in get_all_associates()
                if r["break_start"] is not None and not r["called_out"]]
    return sorted(on_break, key=lambda r: r["break_start"] or "")


def get_called_out_associates() -> list[dict]:
    """Associates marked as called out today."""
    return [r for r in get_all_associates() if r["called_out"]]


def get_associate(associate_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("""
            SELECT
                a.id,
                a.name,
                a.shift,
                a.notes,
                a.called_out,
                b.id           AS break_id,
                b.break_start  AS break_start,
                b.break_type   AS break_type
            FROM associates a
            LEFT JOIN breaks b
                   ON b.associate_id = a.id AND b.break_end IS NULL
            WHERE a.active = 1 AND a.id = ?
        """, (associate_id,)).fetchone()
    if row is None:
        return None
    enriched = _enrich([row])
    return enriched[0] if enriched else None


def set_called_out(associate_id: int, called_out: bool) -> None:
    """Mark or unmark an associate as called out."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE associates SET called_out = ? WHERE id = ?",
            (1 if called_out else 0, associate_id),
        )


def save_associate_note(associate_id: int, note: str) -> None:
    """Persist free-text notes for an associate."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE associates SET notes = ? WHERE id = ?",
            (note.strip(), associate_id),
        )


def get_management_notes() -> str:
    """Return the singleton management note content."""
    with get_conn() as conn:
        row = conn.execute("SELECT content FROM management_notes WHERE id = 1").fetchone()
        return row["content"] if row else ""


def save_management_notes(content: str) -> None:
    """Upsert the singleton management notes."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE management_notes SET content = ?, updated_at = ? WHERE id = 1",
            (content.strip(), datetime.now().isoformat(timespec="seconds")),
        )


def verify_manager_pin(pin: str) -> bool:
    """Return True if the supplied PIN matches the stored one."""
    with get_conn() as conn:
        row = conn.execute("SELECT pin FROM management_notes WHERE id = 1").fetchone()
        return bool(row and row["pin"] == pin.strip())


def set_manager_pin(new_pin: str) -> None:
    """Overwrite the manager PIN."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE management_notes SET pin = ? WHERE id = 1",
            (new_pin.strip(),),
        )


def add_associate(name: str, shift: str | None = None) -> sqlite3.Row:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO associates (name, shift) VALUES (?, ?)",
            (name.strip(), shift),
        )
        new_id = cur.lastrowid
    return get_associate(new_id)


def bulk_import_associates(entries: list[tuple[str, str | None]]) -> dict:
    """
    Insert associates from (name, shift) tuples, skipping duplicates.
    On re-import, updates the shift time for existing associates.

    Returns:
        {"added": [...names...], "updated": [...names...], "skipped": [...names...]}
    """
    with get_conn() as conn:
        existing = {
            row[0].lower(): row[1]
            for row in conn.execute(
                "SELECT name, id FROM associates WHERE active = 1"
            ).fetchall()
        }

    added, updated, skipped = [], [], []
    for name, shift in entries:
        key = name.lower()
        if key in existing:
            # Update the shift time on re-import
            associate_id = existing[key]
            with get_conn() as conn:
                conn.execute(
                    "UPDATE associates SET shift = ? WHERE id = ?",
                    (shift, associate_id),
                )
            updated.append(name)
        else:
            add_associate(name, shift)
            added.append(name)

    return {"added": added, "updated": updated, "skipped": skipped}


def remove_associate(associate_id: int) -> None:
    with get_conn() as conn:
        # Close any open break first
        conn.execute(
            "UPDATE breaks SET break_end = ? WHERE associate_id = ? AND break_end IS NULL",
            (datetime.now().isoformat(timespec="seconds"), associate_id),
        )
        conn.execute(
            "UPDATE associates SET active = 0 WHERE id = ?",
            (associate_id,),
        )


# ── Breaks ──────────────────────────────────────────────────────────────────

def start_break(associate_id: int, break_type: str = "break") -> sqlite3.Row:
    """Start a break or lunch. break_type must be 'break' or 'lunch'."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO breaks (associate_id, break_start, break_type) VALUES (?, ?, ?)",
            (associate_id, datetime.now().isoformat(timespec="seconds"), break_type),
        )
    return get_associate(associate_id)


def end_break(associate_id: int) -> sqlite3.Row:
    with get_conn() as conn:
        conn.execute(
            "UPDATE breaks SET break_end = ? WHERE associate_id = ? AND break_end IS NULL",
            (datetime.now().isoformat(timespec="seconds"), associate_id),
        )
    return get_associate(associate_id)


def get_break_log(limit: int = 30) -> list[sqlite3.Row]:
    """Return completed breaks/lunches, most recent first."""
    with get_conn() as conn:
        return conn.execute("""
            SELECT
                b.id,
                a.name,
                b.break_type,
                b.break_start,
                b.break_end,
                ROUND(
                    (JULIANDAY(b.break_end) - JULIANDAY(b.break_start)) * 1440
                ) AS duration_mins
            FROM breaks b
            JOIN associates a ON a.id = b.associate_id
            WHERE b.break_end IS NOT NULL
            ORDER BY b.break_end DESC
            LIMIT ?
        """, (limit,)).fetchall()


def get_break_log_grouped(limit: int = 60) -> list[dict]:
    """
    Return completed breaks grouped by associate, most-recently-active first.
    Each entry: {name, breaks: [row, ...], total_breaks, total_mins}
    """
    rows = get_break_log(limit)
    groups: dict[str, dict] = {}
    order:  list[str]       = []
    for row in rows:
        name = row["name"]
        if name not in groups:
            groups[name] = {"name": name, "breaks": [], "total_mins": 0}
            order.append(name)
        groups[name]["breaks"].append(row)
        groups[name]["total_mins"] += int(row["duration_mins"] or 0)
    for g in groups.values():
        g["total_breaks"] = len(g["breaks"])
    return [groups[n] for n in order]


def get_associate_breaks_today(associate_id: int) -> list[sqlite3.Row]:
    """Return all breaks (completed + open) for an associate today."""
    today = datetime.now().strftime("%Y-%m-%d")
    with get_conn() as conn:
        return conn.execute("""
            SELECT
                b.break_type,
                b.break_start,
                b.break_end,
                CASE WHEN b.break_end IS NOT NULL
                     THEN ROUND((JULIANDAY(b.break_end) - JULIANDAY(b.break_start)) * 1440)
                     ELSE NULL
                END AS duration_mins
            FROM breaks b
            WHERE b.associate_id = ?
              AND DATE(b.break_start) = ?
            ORDER BY b.break_start
        """, (associate_id, today)).fetchall()


def _mins_to_clock(minutes: int) -> str:
    """Convert minutes-since-midnight to a readable 12-hour clock string."""
    h, m = divmod(minutes, 60)
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d}{suffix}"


def suggested_breaks(shift: str | None) -> list[str]:
    """
    Return a list of suggested 15-min break start times for the shift.

    Rule:
      - Shift < 6 hours  → 1 break at start + 2 hours
      - Shift >= 6 hours → 2 breaks: start + 2 hours  AND  end - 2 hours
    """
    if not shift:
        return []

    start = _shift_start_minutes(shift)
    end   = _shift_end_minutes(shift)

    if start >= 9999 or end >= 9999 or end <= start:
        return []

    duration_hrs = (end - start) / 60

    first  = start + 120          # 2 hours after shift start
    second = end   - 120          # 2 hours before shift end

    if duration_hrs < 6:
        return [_mins_to_clock(first)]

    # Guard against overlap (very short 6-hr shifts where first >= second)
    if first >= second:
        mid = (start + end) // 2
        return [_mins_to_clock(mid)]

    return [_mins_to_clock(first), _mins_to_clock(second)]


# ── Stats ─────────────────────────────────────────────────────────────────

def get_stats() -> dict:
    with get_conn() as conn:
        total     = conn.execute("SELECT COUNT(*) FROM associates WHERE active=1").fetchone()[0]
        on_break  = conn.execute(
            "SELECT COUNT(*) FROM breaks WHERE break_end IS NULL"
        ).fetchone()[0]
        working   = total - on_break
        return {"total": total, "on_break": on_break, "working": working}


def reset_for_new_day() -> None:
    """
    Prepare the roster for a fresh day:
      - Delete all break history.
      - Close any open breaks first (so FK constraints are satisfied).
      - Soft-delete all associates so the screen is clear.
    """
    now = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            "UPDATE breaks SET break_end = ? WHERE break_end IS NULL", (now,)
        )
        conn.execute("DELETE FROM breaks")
        conn.execute("UPDATE associates SET active = 0")


def get_manager_report() -> dict:
    """
    Returns data for the manager dashboard:
      overruns  — breaks that exceeded 15 minutes
      no_breaks — active associates who took zero 15-min breaks today
      notes     — all active associates who have a non-empty note
    """
    today = datetime.now().strftime("%Y-%m-%d")
    with get_conn() as conn:
        # Completed breaks that ran over 15 min (type='break' only, not lunch)
        overruns = conn.execute("""
            SELECT
                a.name,
                a.shift,
                b.break_start,
                b.break_end,
                ROUND(
                    (JULIANDAY(b.break_end) - JULIANDAY(b.break_start)) * 1440
                ) AS duration_mins
            FROM breaks b
            JOIN associates a ON a.id = b.associate_id
            WHERE b.break_end IS NOT NULL
              AND b.break_type = 'break'
              AND DATE(b.break_start) = ?
              AND (JULIANDAY(b.break_end) - JULIANDAY(b.break_start)) * 1440 > 15
            ORDER BY duration_mins DESC
        """, (today,)).fetchall()

        # Active associates who have taken zero 15-min breaks today
        all_active = conn.execute(
            "SELECT id, name, shift FROM associates WHERE active = 1"
        ).fetchall()

        no_breaks = []
        for assoc in all_active:
            allowed = len(suggested_breaks(assoc["shift"]))
            if allowed == 0:
                continue   # no shift on file — can't judge
            taken = conn.execute("""
                SELECT COUNT(*) FROM breaks
                WHERE associate_id = ?
                  AND break_type   = 'break'
                  AND break_end    IS NOT NULL
                  AND DATE(break_start) = ?
            """, (assoc["id"], today)).fetchone()[0]
            if taken == 0:
                no_breaks.append(dict(assoc))

        # Associates with non-empty notes (active only)
        notes_rows = conn.execute("""
            SELECT name, shift, notes
            FROM associates
            WHERE active = 1
              AND notes IS NOT NULL
              AND TRIM(notes) != ''
            ORDER BY name
        """).fetchall()

    return {
        "overruns":   [dict(r) for r in overruns],
        "no_breaks":  no_breaks,
        "notes":      [dict(r) for r in notes_rows],
        "mgmt_notes": get_management_notes(),
        "date":       datetime.now().strftime("%B %d, %Y"),
    }
