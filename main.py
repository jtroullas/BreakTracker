"""
main.py — FastAPI routes for Break Tracker.
Uses raw Jinja2 instead of Starlette's templating wrapper to sidestep
the 1.x API churn. Same result, zero drama.

Stats / log refresh strategy:
  - Card actions fire the `breakChanged` event via HX-Trigger header.
  - The stats bar and log listen for `breakChanged from:body` so they
    auto-refresh without any OOB soup. Clean!
"""
import json

from pathlib import Path

import jinja2
from fastapi import FastAPI, Form, Response, UploadFile, File
from fastapi.responses import HTMLResponse, PlainTextResponse

import database as db
import pdf_parser

# ── Jinja2 setup (raw, no Starlette wrapper) ─────────────────────────────────
TEMPLATES_DIR = Path(__file__).parent / "templates"
_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=jinja2.select_autoescape(["html"]),
)

# Expose shift helpers + suggested breaks to every template
_jinja_env.globals["shift_end_minutes"]  = db._shift_end_minutes
_jinja_env.globals["suggested_breaks"]   = db.suggested_breaks

app = FastAPI(title="Break Tracker")


@app.on_event("startup")
def on_startup():
    db.init_db()


def render(template_name: str, ctx: dict = None) -> HTMLResponse:
    """Render a Jinja2 template and return an HTMLResponse."""
    tmpl = _jinja_env.get_template(template_name)
    html = tmpl.render(**(ctx or {}))
    return HTMLResponse(html)


# ── Pages ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    return render("index.html", {
        "working":    db.get_working_associates(),
        "on_break":   db.get_on_break_associates(),
        "called_out": db.get_called_out_associates(),
        "log":        db.get_break_log_grouped(),
        "stats":      db.get_stats(),
    })


# ── Associate management ─────────────────────────────────────────────────────

@app.post("/associates", response_class=HTMLResponse)
def add_associate(response: Response, name: str = Form(...)):
    associate = db.add_associate(name)
    response.headers["HX-Trigger"] = "breakChanged"
    return render("partials/associate_card.html", {"associate": associate})


@app.delete("/associates/{associate_id}", response_class=HTMLResponse)
def remove_associate(associate_id: int, response: Response):
    db.remove_associate(associate_id)
    response.headers["HX-Trigger"] = "breakChanged"
    return HTMLResponse("")


# ── PDF Schedule Import ──────────────────────────────────────────────────────

@app.post("/associates/import-pdf", response_class=HTMLResponse)
async def import_pdf(response: Response, file: UploadFile = File(...)):
    """
    Parse a schedule PDF, extract associate names, bulk-insert new ones.
    Fires gridRefresh + breakChanged so the grid and stats update automatically.
    """
    if not file.filename.lower().endswith(".pdf"):
        return render("partials/import_result.html", {
            "error": "Please upload a PDF file.",
        })

    pdf_bytes = await file.read()

    try:
        entries = pdf_parser.extract_names(pdf_bytes)  # [(name, shift), ...]
    except Exception as exc:
        return render("partials/import_result.html", {
            "error": f"Could not read PDF: {exc}",
        })

    result = db.bulk_import_associates(entries)

    # Fire both events — grid re-fetches /partials/grid, stats re-fetches /partials/stats
    response.headers["HX-Trigger"] = json.dumps(
        {"gridRefresh": True, "breakChanged": True}
    )
    return render("partials/import_result.html", {"result": result})


@app.post("/associates/debug-pdf", response_class=HTMLResponse)
async def debug_pdf(file: UploadFile = File(...)):
    """
    Return raw extracted text + detected names so a team leader can verify
    their PDF format is being parsed correctly.
    """
    pdf_bytes = await file.read()
    try:
        raw     = pdf_parser.extract_raw_text(pdf_bytes)
        entries = pdf_parser.extract_names(pdf_bytes)
    except Exception as exc:
        return HTMLResponse(f"<p class='text-red-600 text-sm'>Error: {exc}</p>")

    names_html = (
        "<ul class='list-disc pl-4 text-sm space-y-0.5'>" +
        "".join(
            f"<li><strong>{n}</strong>"
            + (f" <span class='text-gray-400 font-mono'>{s}</span>" if s else "")
            + "</li>"
            for n, s in entries
        ) +
        "</ul>"
        if entries else "<p class='text-sm text-gray-500'>No names detected.</p>"
    )
    raw_escaped = raw.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return HTMLResponse(
        f"""<div class='space-y-3'>
          <div>
            <p class='text-sm font-semibold text-gray-700 mb-1'>Detected names ({len(entries)}):</p>
            {names_html}
          </div>
          <details class='text-xs'>
            <summary class='cursor-pointer text-gray-400 hover:text-gray-600'>Raw PDF text</summary>
            <pre class='mt-2 bg-gray-50 rounded p-2 overflow-x-auto whitespace-pre-wrap text-gray-600
                        border border-gray-200'>{raw_escaped[:4000]}</pre>
          </details>
        </div>"""
    )


# ── Break management ───────────────────────────────────────────────────────────

@app.post("/associates/{associate_id}/break/start", response_class=HTMLResponse)
def break_start(associate_id: int, response: Response):
    associate = db.start_break(associate_id, break_type="break")
    response.headers["HX-Trigger"] = "breakChanged"
    return render("partials/associate_card.html", {"associate": associate})


@app.post("/associates/{associate_id}/lunch/start", response_class=HTMLResponse)
def lunch_start(associate_id: int, response: Response):
    associate = db.start_break(associate_id, break_type="lunch")
    response.headers["HX-Trigger"] = "breakChanged"
    return render("partials/associate_card.html", {"associate": associate})


@app.post("/associates/{associate_id}/break/end", response_class=HTMLResponse)
def break_end(associate_id: int, response: Response):
    associate = db.end_break(associate_id)
    response.headers["HX-Trigger"] = "breakChanged"
    return render("partials/associate_card.html", {"associate": associate})


# ── Partials (polled / triggered by HTMX) ───────────────────────────────────

@app.get("/partials/stats", response_class=HTMLResponse)
def stats_partial():
    return render("partials/stats_bar.html", {"stats": db.get_stats()})


@app.get("/partials/log", response_class=HTMLResponse)
def log_partial():
    return render("partials/break_log.html", {"log": db.get_break_log_grouped()})


@app.get("/associates/{associate_id}/detail", response_class=HTMLResponse)
def associate_detail(associate_id: int):
    """Modal content: today's breaks + suggested break times for one associate."""
    associate    = db.get_associate(associate_id)
    if not associate:
        return HTMLResponse("<p class='text-red-500 p-4'>Associate not found.</p>")
    breaks_today = db.get_associate_breaks_today(associate_id)
    suggestions  = db.suggested_breaks(associate["shift"])
    return render("partials/associate_detail.html", {
        "associate":    associate,
        "breaks_today": breaks_today,
        "suggestions":  suggestions,
    })


@app.post("/associates/{associate_id}/note", response_class=HTMLResponse)
def save_note(associate_id: int, note: str = Form(default="")):
    """Save free-text notes for an associate and return a confirmation snippet."""
    db.save_associate_note(associate_id, note)
    return HTMLResponse(
        "<span class='text-green-600 text-xs font-semibold'>✓ Saved</span>"
    )


@app.get("/partials/grid", response_class=HTMLResponse)
def grid_partial():
    """Working associates only (no open break)."""
    return render("partials/associate_grid.html", {
        "associates": db.get_working_associates(),
    })


@app.get("/partials/on-break", response_class=HTMLResponse)
def on_break_partial():
    return render("partials/on_break_section.html", {
        "on_break": db.get_on_break_associates(),
    })


@app.get("/partials/called-out", response_class=HTMLResponse)
def called_out_partial():
    return render("partials/called_out_section.html", {
        "called_out": db.get_called_out_associates(),
    })


@app.post("/reset", response_class=HTMLResponse)
def reset_day():
    """Close all open breaks, wipe history, clear roster."""
    db.reset_for_new_day()
    return HTMLResponse("")


@app.post("/associates/{associate_id}/callout", response_class=HTMLResponse)
def callout(associate_id: int):
    db.set_called_out(associate_id, True)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@app.post("/associates/{associate_id}/callout/clear", response_class=HTMLResponse)
def callout_clear(associate_id: int):
    db.set_called_out(associate_id, False)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


# ── Manager dashboard ─────────────────────────────────────────────────────────

@app.get("/manager", response_class=HTMLResponse)
def manager_dashboard():
    report = db.get_manager_report()
    return render("manager.html", {"report": report})


@app.post("/manager/auth", response_class=HTMLResponse)
def manager_auth(pin: str = Form(...)):
    """Validate the manager PIN. Returns JSON-ish fragment HTMX reads."""
    if db.verify_manager_pin(pin):
        return HTMLResponse("ok")
    return HTMLResponse("fail", status_code=401)


@app.post("/manager/notes", response_class=HTMLResponse)
def save_mgmt_notes(note: str = Form(default="")):
    """Persist management-only notes (not visible to team leaders)."""
    db.save_management_notes(note)
    return HTMLResponse(
        "<span class='text-green-600 text-xs font-semibold'>✓ Saved</span>"
    )


@app.post("/manager/change-pin", response_class=HTMLResponse)
def change_pin(
    current_pin: str = Form(...),
    new_pin:     str = Form(...),
    confirm_pin: str = Form(...),
):
    """Change the manager PIN after verifying the current one."""
    if not db.verify_manager_pin(current_pin):
        return HTMLResponse(
            "<span class='text-red-500 text-xs font-semibold'>✕ Current PIN is incorrect.</span>"
        )
    if len(new_pin.strip()) < 4:
        return HTMLResponse(
            "<span class='text-red-500 text-xs font-semibold'>✕ PIN must be at least 4 digits.</span>"
        )
    if new_pin != confirm_pin:
        return HTMLResponse(
            "<span class='text-red-500 text-xs font-semibold'>✕ New PINs do not match.</span>"
        )
    db.set_manager_pin(new_pin)
    return HTMLResponse(
        "<span class='text-green-600 text-xs font-semibold'>✓ PIN updated successfully.</span>"
    )
