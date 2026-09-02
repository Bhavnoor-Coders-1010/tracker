"""
Anushasan — UPSC + Academics Tracker
Single-file FastAPI app. See UPSC_Tracker_App_Plan.md for the full design doc.
"""

# ---------------------------------------------------------------------------
# ---- SECTION: Imports & Config ----
# ---------------------------------------------------------------------------
import os
import json
import base64
import calendar
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, date, timedelta
from typing import Optional, List
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from apscheduler.schedulers.background import BackgroundScheduler
from pydantic import BaseModel

TZ = ZoneInfo("Asia/Kolkata")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_DATA_REPO = os.environ.get("GITHUB_DATA_REPO", "")  # "username/tracker-data"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_DATA_REPO}/contents/data.json"

GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
NOTIFY_TO = os.environ.get("NOTIFY_TO", GMAIL_USER)

# Used to build the link in the daily-review email. Set this to your
# https://<service>.onrender.com URL once you know it (Render env var).
APP_BASE_URL = os.environ.get("APP_BASE_URL", "")

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DAY_CODE = {"Monday": "mon", "Tuesday": "tue", "Wednesday": "wed", "Thursday": "thu",
            "Friday": "fri", "Saturday": "sat", "Sunday": "sun"}
MONTH_CYCLE = ["August", "September", "October", "November", "December",
               "January", "February", "March", "April", "May"]
MONTH_NUM = {"January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
             "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12}

BEHIND_PACE_THRESHOLD = 15.0  # required %/week above this is flagged "behind pace"

app = FastAPI(title="Anushasan — UPSC + Academics Tracker")


# ---------------------------------------------------------------------------
# ---- SECTION: Models ----
# ---------------------------------------------------------------------------
class Book(BaseModel):
    name: str
    status: str = "not_started"          # not_started | in_progress | done | ongoing
    pct: Optional[int] = 0
    target_month: Optional[str] = None


class Subject(BaseModel):
    books: List[Book] = []


class TimetableBlock(BaseModel):
    start: str
    end: str
    activity: str


class ExamDate(BaseModel):
    label: str
    date: str


class DailyLog(BaseModel):
    planned_blocks: List[str] = []
    completed_blocks: List[str] = []
    notes: str = ""


# ---------------------------------------------------------------------------
# ---- SECTION: GitHub-backed storage ("git as database") ----
# Reads/writes data.json in the separate `tracker-data` repo via the GitHub
# Contents API, instead of a local disk / Fly Volume. See Section 2 of the
# plan for why: free Render web services can't attach persistent disks.
# ---------------------------------------------------------------------------
_sha_cache: Optional[str] = None
_data_cache: Optional[dict] = None


def _headers() -> dict:
    return {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}


def _github_read() -> Optional[dict]:
    global _sha_cache
    resp = requests.get(GITHUB_API_URL, headers=_headers(), timeout=15)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    payload = resp.json()
    _sha_cache = payload["sha"]
    content = base64.b64decode(payload["content"]).decode("utf-8")
    return json.loads(content)


def _github_write(data: dict, message: str) -> None:
    global _sha_cache
    body = {
        "message": message,
        "content": base64.b64encode(
            json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
        ).decode("utf-8"),
    }
    if _sha_cache:
        body["sha"] = _sha_cache
    resp = requests.put(GITHUB_API_URL, headers=_headers(), json=body, timeout=15)
    resp.raise_for_status()
    _sha_cache = resp.json()["content"]["sha"]


def load_data(force: bool = False) -> dict:
    """In-memory cache for the process lifetime; only hits GitHub on first
    load (or when force=True, e.g. from a scheduled job that wants fresh data)."""
    global _data_cache
    if _data_cache is not None and not force:
        return _data_cache
    data = _github_read()
    if data is None:
        seed_path = os.path.join(os.path.dirname(__file__), "seed_data.json")
        with open(seed_path, encoding="utf-8") as f:
            data = json.load(f)
        _github_write(data, "Initialize tracker data from seed")
    _data_cache = data
    return data


def save_data(data: dict, message: str = "Update tracker data") -> None:
    global _data_cache
    _github_write(data, message)
    _data_cache = data


# ---------------------------------------------------------------------------
# ---- SECTION: Data helpers (Aug–May cycle dates, run-rate calc) ----
# ---------------------------------------------------------------------------
def cycle_start_year(today: date) -> int:
    """The Aug-May plan spans a year boundary; returns the year 'August' falls in."""
    return today.year if today.month >= 8 else today.year - 1


def target_month_end_date(month_name: str, today: date) -> date:
    start_year = cycle_start_year(today)
    idx = MONTH_CYCLE.index(month_name)
    year = start_year if idx <= 4 else start_year + 1  # Aug..Dec -> start_year, Jan..May -> +1
    month_num = MONTH_NUM[month_name]
    last_day = calendar.monthrange(year, month_num)[1]
    return date(year, month_num, last_day)


def run_rate(book: dict, today: date) -> Optional[dict]:
    """Returns {'required_pct_per_week': float, 'behind_pace': bool}, or None
    if this book has no target month or is an 'ongoing' tracker with no %."""
    if book.get("pct") is None or not book.get("target_month"):
        return None
    remaining = max(100 - book["pct"], 0)
    if remaining == 0:
        return {"required_pct_per_week": 0.0, "behind_pace": False}
    end_date = target_month_end_date(book["target_month"], today)
    weeks_left = max((end_date - today).days / 7, 0.1)
    required = remaining / weeks_left
    return {"required_pct_per_week": round(required, 1), "behind_pace": required > BEHIND_PACE_THRESHOLD}


def todays_blocks(data: dict, on: Optional[date] = None) -> List[dict]:
    on = on or datetime.now(TZ).date()
    day_name = DAY_NAMES[on.weekday()]
    return data.get("timetable", {}).get(day_name, [])


def remaining_blocks_today(data: dict) -> List[dict]:
    now = datetime.now(TZ)
    out = []
    for b in todays_blocks(data):
        end_h, end_m = b["end"].split(":")
        end_h = int(end_h)
        end_m = int(end_m)
        if end_h >= 24:
            end_h, end_m = 23, 59
        block_end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
        if block_end >= now:
            out.append(b)
    return out


# ---------------------------------------------------------------------------
# ---- SECTION: Email ----
# ---------------------------------------------------------------------------
def send_email(subject: str, body: str, to: Optional[str] = None) -> None:
    to = to or NOTIFY_TO
    if not GMAIL_USER or not GMAIL_APP_PASSWORD or not to:
        print(f"[email skipped — missing GMAIL_USER/GMAIL_APP_PASSWORD/recipient] {subject}")
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.send_message(msg)


# ---------------------------------------------------------------------------
# ---- SECTION: Scheduler ----
# All jobs only fire while the process is awake — see the keep-alive note
# in the plan's Section 2. Times are in Asia/Kolkata.
# ---------------------------------------------------------------------------
scheduler = BackgroundScheduler(timezone=TZ)


def job_block_reminder(activity: str, start: str, end: str) -> None:
    send_email(f"Next: {activity}", f"Next: {activity} · {start}–{end}")


def job_daily_review_prompt() -> None:
    link = f"{APP_BASE_URL}/review" if APP_BASE_URL else "/review"
    send_email(
        "Daily review — what actually happened today?",
        f"Fill in what you actually did vs. planned: {link}",
    )


def job_weekly_digest() -> None:
    data = load_data(force=True)
    today = datetime.now(TZ).date()
    week_ago = today - timedelta(days=7)
    lines = ["Weekly digest", ""]

    planned_total = completed_total = 0
    for day_str, log in data.get("daily_logs", {}).items():
        try:
            d = datetime.strptime(day_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if week_ago <= d <= today:
            planned_total += len(log.get("planned_blocks", []))
            completed_total += len(log.get("completed_blocks", []))
    pct = round(100 * completed_total / planned_total, 1) if planned_total else 0
    lines.append(f"Planned vs completed this week: {completed_total}/{planned_total} blocks ({pct}%)")
    lines.append("")
    lines.append("Run-rate flags (books behind pace):")
    flagged = False
    for subject_name, subject in data.get("subjects", {}).items():
        for book in subject.get("books", []):
            rr = run_rate(book, today)
            if rr and rr["behind_pace"]:
                flagged = True
                lines.append(f"  - {subject_name} / {book['name']}: needs {rr['required_pct_per_week']}%/week")
    if not flagged:
        lines.append("  - none — everything's on pace")

    next_month_num = (today.month % 12) + 1
    next_month_name = next((m for m, n in MONTH_NUM.items() if n == next_month_num), None)
    if next_month_name and next_month_name in data.get("monthly_plan", {}):
        focus = ", ".join(data["monthly_plan"][next_month_name])
        lines.append("")
        lines.append(f"Upcoming month's focus ({next_month_name}): {focus}")

    send_email("Weekly digest", "\n".join(lines))


def job_exam_alerts() -> None:
    data = load_data(force=True)
    today = datetime.now(TZ).date()
    for exam in data.get("exam_dates", []):
        try:
            exam_date = datetime.strptime(exam["date"], "%Y-%m-%d").date()
        except ValueError:
            continue
        if (exam_date - today).days == 2:
            send_email(
                f"{exam['label']} in 2 days",
                f"{exam['label']} starts in 2 days — {exam_date.strftime('%b %d, %Y')}",
            )


def job_monthly_nudge() -> None:
    data = load_data(force=True)
    today = datetime.now(TZ).date()
    month_name = next((m for m, n in MONTH_NUM.items() if n == today.month), None)
    focus = data.get("monthly_plan", {}).get(month_name) if month_name else None
    if focus:
        send_email(f"{month_name} focus", f"{month_name} focus: {', '.join(focus)}")


def schedule_all_jobs() -> None:
    data = load_data()
    scheduler.remove_all_jobs()

    for day_name, blocks in data.get("timetable", {}).items():
        code = DAY_CODE.get(day_name)
        if not code:
            continue
        for block in blocks:
            h, m = map(int, block["start"].split(":"))
            reminder = datetime(2000, 1, 1, h, m) - timedelta(minutes=5)
            scheduler.add_job(
                job_block_reminder, "cron", day_of_week=code,
                hour=reminder.hour, minute=reminder.minute,
                args=[block["activity"], block["start"], block["end"]],
                id=f"block_{day_name}_{block['start']}", replace_existing=True,
            )

    scheduler.add_job(job_daily_review_prompt, "cron", hour=23, minute=45,
                       id="daily_review", replace_existing=True)
    scheduler.add_job(job_weekly_digest, "cron", day_of_week="sun", hour=21, minute=0,
                       id="weekly_digest", replace_existing=True)
    scheduler.add_job(job_exam_alerts, "cron", hour=8, minute=0,
                       id="exam_alerts", replace_existing=True)
    scheduler.add_job(job_monthly_nudge, "cron", day=1, hour=0, minute=5,
                       id="monthly_nudge", replace_existing=True)


# ---------------------------------------------------------------------------
# ---- SECTION: HTML templates (inline — no templates/ folder) ----
# ---------------------------------------------------------------------------
BASE_CSS = """
:root {
  --bg: #14181f; --card: #1d232d; --card-border: #2a3140;
  --ink: #ede7da; --ink-muted: #9aa1ac;
  --brass: #c9a24b; --teal: #5e9e8f; --terracotta: #c4634a;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font-family: 'Inter', system-ui, sans-serif; line-height: 1.5; }
h1, h2, h3 { font-family: 'Source Serif 4', Georgia, serif; font-weight: 600; margin: 0 0 0.4em; }
a { color: var(--brass); text-decoration: none; }
a:hover { text-decoration: underline; }
header { padding: 1.5rem 1.5rem 1rem; border-bottom: 1px solid var(--card-border);
  display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 0.5rem; }
header nav a { margin-left: 1.2rem; color: var(--ink-muted); font-size: 0.9rem; }
header nav a:hover { color: var(--ink); }
main { max-width: 720px; margin: 0 auto; padding: 1.5rem; }
.card { background: var(--card); border: 1px solid var(--card-border); border-radius: 10px;
  padding: 1.1rem 1.3rem; margin-bottom: 1rem; }
.progress-track { background: #10141a; border-radius: 6px; height: 10px; overflow: hidden; margin-top: 0.4rem; }
.progress-fill { background: var(--teal); height: 100%; }
.muted { color: var(--ink-muted); font-size: 0.88rem; }
.pill { display: inline-block; padding: 0.15rem 0.6rem; border-radius: 999px; font-size: 0.78rem;
  background: rgba(196,99,74,0.15); color: var(--terracotta); border: 1px solid rgba(196,99,74,0.35); white-space: nowrap; }
input[type=number], input[type=text], select, textarea {
  background: #10141a; border: 1px solid var(--card-border); color: var(--ink);
  border-radius: 6px; padding: 0.45rem 0.6rem; font-family: inherit; width: 100%; }
textarea { min-height: 160px; font-family: 'IBM Plex Mono', monospace; font-size: 0.85rem; }
label { display: block; margin: 0.6rem 0 0.2rem; font-size: 0.9rem; color: var(--ink-muted); }
button { background: var(--brass); color: #14181f; border: none; border-radius: 6px;
  padding: 0.55rem 1.1rem; font-weight: 600; cursor: pointer; margin-top: 1rem; }
button:hover { filter: brightness(1.08); }
.book-row { display: flex; align-items: center; justify-content: space-between; gap: 0.6rem;
  padding: 0.5rem 0; border-bottom: 1px solid var(--card-border); flex-wrap: wrap; }
.book-row:last-child { border-bottom: none; }
.check-row { display: flex; align-items: center; gap: 0.6rem; padding: 0.35rem 0; }
"""


def page(title: str, body: str) -> HTMLResponse:
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title} · Anushasan</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Source+Serif+4:wght@600;700&display=swap" rel="stylesheet">
  <style>{BASE_CSS}</style>
</head>
<body>
  <header>
    <div><h1 style="font-size:1.3rem;">Anushasan</h1><div class="muted">UPSC + Academics Tracker</div></div>
    <nav>
      <a href="/">Dashboard</a>
      <a href="/review">Review</a>
      <a href="/setup">Setup</a>
    </nav>
  </header>
  <main>{body}</main>
</body>
</html>"""
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# ---- SECTION: Routes ----
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard():
    data = load_data()
    today = datetime.now(TZ).date()

    blocks_html = ""
    for b in remaining_blocks_today(data):
        blocks_html += (
            f'<div class="check-row"><span class="muted" style="width:5.5rem;">'
            f'{b["start"]}–{b["end"]}</span><span>{b["activity"]}</span></div>'
        )
    if not blocks_html:
        blocks_html = '<p class="muted">Nothing left on today&#39;s timetable.</p>'

    subjects_html = ""
    for subject_name, subject in data.get("subjects", {}).items():
        books = subject.get("books", [])
        pct_values = [b["pct"] for b in books if b.get("pct") is not None]
        avg_pct = round(sum(pct_values) / len(pct_values)) if pct_values else 0
        book_rows = ""
        for b in books:
            rr = run_rate(b, today)
            pct_display = f'{b["pct"]}%' if b.get("pct") is not None else "ongoing"
            flag = (f'<span class="pill">{rr["required_pct_per_week"]}%/wk needed</span>'
                    if rr and rr["behind_pace"] else "")
            book_rows += (
                f'<div class="book-row"><span>{b["name"]} '
                f'<span class="muted">({pct_display})</span></span>{flag}</div>'
            )
        subjects_html += f"""<div class="card">
            <h3>{subject_name.replace("_", " ")}</h3>
            <div class="progress-track"><div class="progress-fill" style="width:{avg_pct}%;"></div></div>
            <div class="muted" style="margin-top:0.3rem;">{avg_pct}% average across {len(books)} book(s)</div>
            {book_rows}
        </div>"""

    body = f"""
    <div class="card">
      <h2>Today — {today.strftime("%A, %d %B")}</h2>
      {blocks_html}
    </div>
    {subjects_html}
    """
    return page("Dashboard", body)


@app.get("/setup", response_class=HTMLResponse)
def setup_form():
    data = load_data()
    books_html = ""
    statuses = ["not_started", "in_progress", "done", "ongoing"]
    for subject_name, subject in data.get("subjects", {}).items():
        rows = ""
        for i, b in enumerate(subject.get("books", [])):
            if b.get("status") == "ongoing":
                pct_input = '<span class="muted">ongoing</span>'
            else:
                pct_val = b.get("pct") if b.get("pct") is not None else 0
                pct_input = (f'<input type="number" name="pct__{subject_name}__{i}" '
                             f'min="0" max="100" value="{pct_val}" style="width:5rem;">')
            options = "".join(
                f'<option value="{s}" {"selected" if b.get("status") == s else ""}>{s}</option>'
                for s in statuses
            )
            rows += f"""<div class="book-row">
                <span>{b["name"]}</span>
                <span style="display:flex;gap:0.5rem;align-items:center;">
                  <select name="status__{subject_name}__{i}">{options}</select>
                  {pct_input}
                </span>
            </div>"""
        books_html += f'<div class="card"><h3>{subject_name.replace("_", " ")}</h3>{rows}</div>'

    body = f"""
    <form method="post" action="/setup">
      <h2>Update book progress</h2>
      {books_html}
      <button type="submit">Save progress</button>
    </form>
    <div class="card">
      <h2>Advanced: timetable / exam dates / monthly plan</h2>
      <p class="muted">Raw JSON — edit carefully, this replaces those sections wholesale.</p>
      <form method="post" action="/setup/advanced">
        <label>Timetable</label>
        <textarea name="timetable">{json.dumps(data.get("timetable", {}), indent=2)}</textarea>
        <label>Exam dates</label>
        <textarea name="exam_dates">{json.dumps(data.get("exam_dates", []), indent=2)}</textarea>
        <label>Monthly plan</label>
        <textarea name="monthly_plan">{json.dumps(data.get("monthly_plan", {}), indent=2)}</textarea>
        <button type="submit">Save advanced settings</button>
      </form>
    </div>
    """
    return page("Setup", body)


@app.post("/setup")
async def setup_submit(request: Request):
    form = await request.form()
    data = load_data()
    for key, value in form.items():
        if key.startswith("status__"):
            _, subject_name, idx = key.split("__")
            data["subjects"][subject_name]["books"][int(idx)]["status"] = value
        elif key.startswith("pct__") and value != "":
            _, subject_name, idx = key.split("__")
            data["subjects"][subject_name]["books"][int(idx)]["pct"] = int(value)
    save_data(data, "Update book progress via /setup")
    schedule_all_jobs()
    return RedirectResponse("/setup", status_code=303)


@app.post("/setup/advanced")
async def setup_advanced_submit(request: Request):
    form = await request.form()
    data = load_data()
    try:
        data["timetable"] = json.loads(form["timetable"])
        data["exam_dates"] = json.loads(form["exam_dates"])
        data["monthly_plan"] = json.loads(form["monthly_plan"])
    except json.JSONDecodeError as e:
        return page("Setup — error", f'<div class="card">Invalid JSON: {e}. Go back and fix it.</div>')
    save_data(data, "Update timetable/exam dates/monthly plan via /setup")
    schedule_all_jobs()
    return RedirectResponse("/setup", status_code=303)


@app.get("/review", response_class=HTMLResponse)
def review_form():
    data = load_data()
    today = datetime.now(TZ).date()
    today_str = today.isoformat()
    blocks = todays_blocks(data)
    existing = data.get("daily_logs", {}).get(today_str, {})
    completed = set(existing.get("completed_blocks", []))

    rows = ""
    for b in blocks:
        block_id = f'{b["start"]}-{b["activity"]}'
        checked = "checked" if block_id in completed else ""
        rows += f"""<div class="check-row">
            <input type="checkbox" name="completed" value="{block_id}" {checked} id="{block_id}">
            <label for="{block_id}" style="margin:0;">{b["start"]}–{b["end"]} — {b["activity"]}</label>
        </div>"""

    body = f"""
    <form method="post" action="/review">
      <div class="card">
        <h2>Today&#39;s review — {today.strftime("%A, %d %B")}</h2>
        <p class="muted">Check off what you actually did.</p>
        {rows}
        <label>Notes</label>
        <textarea name="notes" style="min-height:80px;font-family:inherit;">{existing.get("notes", "")}</textarea>
        <button type="submit">Save review</button>
      </div>
    </form>
    """
    return page("Review", body)


@app.post("/review")
async def review_submit(request: Request):
    form = await request.form()
    data = load_data()
    today_str = datetime.now(TZ).date().isoformat()
    blocks = todays_blocks(data)
    planned_ids = [f'{b["start"]}-{b["activity"]}' for b in blocks]
    completed_ids = form.getlist("completed")
    data.setdefault("daily_logs", {})[today_str] = {
        "planned_blocks": planned_ids,
        "completed_blocks": completed_ids,
        "notes": form.get("notes", ""),
    }
    save_data(data, f"Daily review for {today_str}")
    return RedirectResponse("/review", status_code=303)


@app.get("/test-email")
def test_email():
    send_email("Anushasan test email", "If you're reading this, email sending works.")
    ok = bool(GMAIL_USER and GMAIL_APP_PASSWORD)
    return {"status": "sent" if ok else "skipped — missing GMAIL_USER/GMAIL_APP_PASSWORD"}


# ---------------------------------------------------------------------------
# ---- SECTION: Startup ----
# ---------------------------------------------------------------------------
@app.on_event("startup")
def on_startup():
    load_data()
    schedule_all_jobs()
    scheduler.start()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
