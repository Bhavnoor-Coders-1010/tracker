"""
Anushasan - UPSC + Academics Tracker
Single-file FastAPI app. Configuration and run instructions are in README.md.
"""

# ---------------------------------------------------------------------------
# ---- SECTION: Imports & Config ----
# ---------------------------------------------------------------------------
import os
import json
import base64
import calendar
import hashlib
import html as html_module
import re
import smtplib
import threading
from email.mime.text import MIMEText
from datetime import datetime, date, timedelta
from typing import Optional, List
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from apscheduler.schedulers.background import BackgroundScheduler
from pydantic import BaseModel, Field, ValidationError

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
DAILY_REVIEW_TIME = "23:45"  # Asia/Kolkata; the review closes the current day.
DAILY_REVIEW_HOUR, DAILY_REVIEW_MINUTE = (int(v) for v in DAILY_REVIEW_TIME.split(":"))
VALID_STATUSES = {"not_started", "in_progress", "done", "ongoing"}
_BLOCK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
CHAPTER_TOTALS = {
    "Modern History - Spectrum": 39,
    "Ancient & Medieval - Class 11 tamil nadu ncert": 23,
    "Ancient & Medieval - NCERT + notes": 23,
    "Intro to Indian Art (Class 11 NCERT)": 7,
    "Intro to Indian Art (Cl. 11)": 7,
    "Themes in Indian History I & II - NCERT": 8,
    "Themes in Indian History I & II": 8,
    "Indian Culture - Nitin Singhania": 31,
    "NCERT Geography Class 11/12": 37,
    "NCERT Geography 11/12": 37,
    "Indian Constitution at Work - NCERT": 10,
    "Indian Polity - Laxmikanth": 92,
    "Laxmikanth": 92,
    "Political Theory - NCERT - class 11": 8,
    "Political Theory - NCERT": 8,
    "NCERT Economy - class 11": 8,
    "NCERT Economy": 8,
    "Indian Economy - Sanjeev Verma": 30,
    "Ramesh Singh / Sanjeev Verma": 30,
}

app = FastAPI(title="Anushasan - UPSC + Academics Tracker")


# ---------------------------------------------------------------------------
# ---- SECTION: Models ----
# ---------------------------------------------------------------------------
class Book(BaseModel):
    name: str
    status: str = "not_started"          # not_started | in_progress | done | ongoing
    pct: Optional[int] = 0
    target_month: Optional[str] = None
    chapter_total: Optional[int] = None
    chapters_completed: int = 0


class Subject(BaseModel):
    books: List[Book] = Field(default_factory=list)


class TimetableBlock(BaseModel):
    id: Optional[str] = None
    start: str
    end: str
    activity: str


class ExamDate(BaseModel):
    label: str
    date: str


class DailyLog(BaseModel):
    planned_blocks: List[str] = Field(default_factory=list)
    completed_blocks: List[str] = Field(default_factory=list)
    notes: str = ""


# ---------------------------------------------------------------------------
# ---- SECTION: GitHub-backed storage ("git as database") ----
# Reads/writes data.json in the separate `tracker-data` repo via the GitHub
# Contents API, instead of a local disk / Fly Volume. See Section 2 of the
# plan for why: free Render web services can't attach persistent disks.
# ---------------------------------------------------------------------------
_sha_cache: Optional[str] = None
_data_cache: Optional[dict] = None
_storage_lock = threading.RLock()


def _headers() -> dict:
    headers = {"Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    if GITHUB_TOKEN:
        headers["Authorization"] = "Bearer " + GITHUB_TOKEN
    return headers


def _github_configured() -> bool:
    return bool(GITHUB_TOKEN and GITHUB_DATA_REPO)


def _github_read() -> Optional[dict]:
    global _sha_cache
    if not _github_configured():
        _sha_cache = None
        return None
    resp = requests.get(GITHUB_API_URL, headers=_headers(), timeout=15)
    if resp.status_code == 404:
        _sha_cache = None
        return None
    resp.raise_for_status()
    payload = resp.json()
    _sha_cache = payload["sha"]
    content = base64.b64decode(payload["content"]).decode("utf-8")
    return json.loads(content)


def _github_write(data: dict, message: str) -> None:
    global _sha_cache, _data_cache
    if not _github_configured():
        return
    content = base64.b64encode(
        json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    ).decode("utf-8")
    with _storage_lock:
        for attempt in range(2):
            if _sha_cache is None:
                _github_read()
            body = {"message": message, "content": content}
            if _sha_cache:
                body["sha"] = _sha_cache
            resp = requests.put(GITHUB_API_URL, headers=_headers(), json=body, timeout=15)
            if resp.status_code == 409 and attempt == 0:
                _sha_cache = None
                _data_cache = None
                _github_read()
                continue
            if resp.status_code == 409:
                _sha_cache = None
                _data_cache = None
                raise RuntimeError("GitHub data changed concurrently; please retry")
            resp.raise_for_status()
            _sha_cache = resp.json()["content"]["sha"]
            return


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
        data = normalize_data(data)
        _github_write(data, "Initialize tracker data from seed")
    else:
        data = normalize_data(data)
    _data_cache = data
    return data


def save_data(data: dict, message: str = "Update tracker data") -> None:
    global _data_cache
    data = normalize_data(data)
    _github_write(data, message)
    _data_cache = data


# ---------------------------------------------------------------------------
# ---- SECTION: Data helpers (Aug-May cycle dates, run-rate calc) ----
# ---------------------------------------------------------------------------
def _text(value: object, default: str = "", limit: int = 500) -> str:
    if value is None:
        return default
    text = str(value).strip()[:limit]
    return text or default


def _model_dump(model: BaseModel) -> dict:
    return model.model_dump() if hasattr(model, "model_dump") else model.dict()


def _clock_minutes(value: object, allow_24: bool = False) -> int:
    text = _text(value)
    parts = text.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ValueError("time must be HH:MM")
    hour, minute = (int(part) for part in parts)
    if minute > 59 or hour < 0 or hour > (24 if allow_24 else 23):
        raise ValueError("time must be in the 00:00-23:59 range")
    if hour == 24 and minute != 0:
        raise ValueError("24:00 is the only valid 24-hour time")
    return hour * 60 + minute


def _block_id(day_name: str, block: dict, index: int) -> str:
    existing = _text(block.get("id"))
    if _BLOCK_ID_RE.fullmatch(existing):
        return existing
    seed = "|".join((_text(day_name), _text(block.get("start")),
                     _text(block.get("end")), _text(block.get("activity")), str(index)))
    return "b_" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


def _normalize_timetable(raw: object) -> dict:
    result = {}
    used_ids = set()
    if not isinstance(raw, dict):
        raw = {}
    for day_name in DAY_NAMES:
        blocks = raw.get(day_name, [])
        if not isinstance(blocks, list):
            continue
        normalized = []
        for index, raw_block in enumerate(blocks):
            if not isinstance(raw_block, dict):
                continue
            try:
                start = _text(raw_block.get("start"))
                end = _text(raw_block.get("end"))
                start_minutes = _clock_minutes(start)
                end_minutes = _clock_minutes(end, allow_24=True)
                if end_minutes == start_minutes and end != "24:00":
                    raise ValueError("block must have a positive duration")
                activity = _text(raw_block.get("activity"), limit=200)
                if not activity:
                    raise ValueError("activity is required")
                block_id = _block_id(day_name, raw_block, index)
                if block_id in used_ids:
                    block_id = "b_" + hashlib.sha1(
                        f"{day_name}|{start}|{end}|{activity}|{index}".encode("utf-8")
                    ).hexdigest()[:12]
                used_ids.add(block_id)
                candidate = TimetableBlock(
                    id=block_id,
                    start=start,
                    end=end,
                    activity=activity,
                )
            except (ValueError, ValidationError):
                continue
            normalized.append(_model_dump(candidate))
        if normalized:
            result[day_name] = normalized
    return result


def normalize_data(raw: object) -> dict:
    """Coerce persisted JSON into the shape used by every route and job."""
    source = raw if isinstance(raw, dict) else {}
    result = dict(source)
    subjects = {}
    raw_subjects = source.get("subjects", {})
    if isinstance(raw_subjects, dict):
        for subject_name, raw_subject in raw_subjects.items():
            if not isinstance(raw_subject, dict):
                continue
            books = []
            raw_books = raw_subject.get("books", [])
            for raw_book in raw_books if isinstance(raw_books, list) else []:
                if not isinstance(raw_book, dict):
                    continue
                try:
                    raw_pct = raw_book.get("pct", 0)
                    pct = None if raw_pct is None else max(0, min(100, int(raw_pct)))
                    book_name = _text(raw_book.get("name"), "Untitled book", 200)
                    chapter_total = raw_book.get("chapter_total")
                    if chapter_total is None:
                        chapter_total = CHAPTER_TOTALS.get(book_name)
                    chapter_total = (
                        max(1, int(chapter_total)) if chapter_total is not None else None
                    )
                    chapters_completed = max(
                        0, min(chapter_total or 0, int(raw_book.get("chapters_completed", 0)))
                    )
                    status = _text(raw_book.get("status"), "not_started")
                    if status not in VALID_STATUSES:
                        status = "not_started"
                    if status == "ongoing":
                        pct = None
                    elif chapter_total:
                        pct = round(100 * chapters_completed / chapter_total)
                        if pct >= 100:
                            status = "done"
                    book = Book(
                        name=book_name,
                        status=status,
                        pct=pct,
                        target_month=(
                            _text(raw_book.get("target_month"))
                            if raw_book.get("target_month") in MONTH_CYCLE else None
                        ),
                        chapter_total=chapter_total,
                        chapters_completed=chapters_completed,
                    )
                except (TypeError, ValueError, ValidationError):
                    continue
                books.append(_model_dump(book))
            subjects[_text(subject_name, "Untitled subject", 100)] = {"books": books}
    result["subjects"] = subjects
    result["timetable"] = _normalize_timetable(source.get("timetable", {}))

    exams = []
    raw_exams = source.get("exam_dates", [])
    for raw_exam in raw_exams if isinstance(raw_exams, list) else []:
        if not isinstance(raw_exam, dict):
            continue
        label = _text(raw_exam.get("label"), "Exam", 200)
        exam_date = _text(raw_exam.get("date"))
        try:
            datetime.strptime(exam_date, "%Y-%m-%d")
        except ValueError:
            continue
        exams.append(_model_dump(ExamDate(label=label, date=exam_date)))
    result["exam_dates"] = exams

    monthly = {}
    raw_monthly = source.get("monthly_plan", {})
    if isinstance(raw_monthly, dict):
        for month, focus in raw_monthly.items():
            if isinstance(focus, list):
                monthly[_text(month, limit=40)] = [
                    _text(item, limit=200) for item in focus if _text(item)
                ]
    result["monthly_plan"] = monthly

    logs = {}
    raw_logs = source.get("daily_logs", {})
    if isinstance(raw_logs, dict):
        for day, raw_log in raw_logs.items():
            try:
                datetime.strptime(str(day), "%Y-%m-%d")
            except ValueError:
                continue
            if not isinstance(raw_log, dict):
                continue
            planned = raw_log.get("planned_blocks", [])
            completed = raw_log.get("completed_blocks", [])
            logs[str(day)] = _model_dump(DailyLog(
                planned_blocks=[_text(v, limit=80) for v in planned if _text(v)]
                if isinstance(planned, list) else [],
                completed_blocks=[_text(v, limit=80) for v in completed if _text(v)]
                if isinstance(completed, list) else [],
                notes=_text(raw_log.get("notes"), limit=5000),
            ))
    result["daily_logs"] = logs
    return result


def validate_advanced_sections(timetable: object, exam_dates: object,
                               monthly_plan: object) -> tuple:
    if not isinstance(timetable, dict):
        raise ValueError("Timetable must be a JSON object")
    unknown_days = set(timetable) - set(DAY_NAMES)
    if unknown_days:
        raise ValueError("Unknown timetable day: " + ", ".join(sorted(unknown_days)))
    checked_timetable = {}
    for day_name, blocks in timetable.items():
        if not isinstance(blocks, list):
            raise ValueError(f"{day_name} timetable must be a list")
        checked = []
        occupied = []
        for raw_block in blocks:
            if not isinstance(raw_block, dict):
                raise ValueError(f"{day_name} contains a non-object block")
            start = _text(raw_block.get("start"))
            end = _text(raw_block.get("end"))
            start_minutes = _clock_minutes(start)
            end_minutes = _clock_minutes(end, allow_24=True)
            if end_minutes == start_minutes and end != "24:00":
                raise ValueError(f"{day_name} block must have a positive duration")
            activity = _text(raw_block.get("activity"), limit=200)
            if not activity:
                raise ValueError(f"{day_name} block activity is required")
            effective_end = end_minutes if end_minutes > start_minutes else end_minutes + 24 * 60
            if any(start_minutes < existing_end and effective_end > existing_start
                   for existing_start, existing_end in occupied):
                raise ValueError(f"{day_name} contains overlapping timetable blocks")
            occupied.append((start_minutes, effective_end))
            checked.append(dict(raw_block, start=start, end=end, activity=activity))
        checked_timetable[day_name] = checked
    if not isinstance(exam_dates, list):
        raise ValueError("Exam dates must be a JSON list")
    checked_exams = []
    for exam in exam_dates:
        if not isinstance(exam, dict):
            raise ValueError("Each exam date must be an object")
        exam_date = _text(exam.get("date"))
        try:
            datetime.strptime(exam_date, "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"Invalid exam date: {exam_date}")
        checked_exams.append(dict(exam, label=_text(exam.get("label"), "Exam", 200),
                                  date=exam_date))
    if not isinstance(monthly_plan, dict):
        raise ValueError("Monthly plan must be a JSON object")
    checked_monthly = {}
    for month, focus in monthly_plan.items():
        if not isinstance(focus, list):
            raise ValueError(f"{month} monthly plan must be a list")
        checked_monthly[_text(month, limit=40)] = [
            _text(item, limit=200) for item in focus if _text(item)
        ]
    return checked_timetable, checked_exams, checked_monthly


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
    try:
        end_date = target_month_end_date(book["target_month"], today)
    except (KeyError, ValueError):
        return None
    weeks_left = max((end_date - today).days / 7, 0.1)
    required = remaining / weeks_left
    return {"required_pct_per_week": round(required, 1), "behind_pace": required > BEHIND_PACE_THRESHOLD}


def todays_blocks(data: dict, on: Optional[date] = None) -> List[dict]:
    on = on or datetime.now(TZ).date()
    day_name = DAY_NAMES[on.weekday()]
    blocks = data.get("timetable", {}).get(day_name, [])
    return [block for block in blocks if isinstance(block, dict)]


def remaining_blocks_today(data: dict) -> List[dict]:
    now = datetime.now(TZ)
    logs = data.get("daily_logs", {})
    today_log = logs.get(now.date().isoformat(), {}) if isinstance(logs, dict) else {}
    completed = set(today_log.get("completed_blocks", [])) if isinstance(today_log, dict) else set()
    for block in todays_blocks(data, now.date()):
        if f'{block.get("start")}-{block.get("activity")}' in completed:
            completed.add(block.get("id"))
    out = []
    for b in todays_blocks(data):
        if b.get("id") in completed:
            continue
        try:
            start_minutes = _clock_minutes(b["start"])
            end_minutes = _clock_minutes(b["end"], allow_24=True)
        except (KeyError, ValueError):
            continue
        if end_minutes <= start_minutes:
            end_minutes += 24 * 60
        block_end = datetime.combine(
            now.date(), datetime.min.time(), TZ
        ) + timedelta(minutes=end_minutes)
        if block_end >= now:
            out.append(b)
    return out


# ---------------------------------------------------------------------------
# ---- SECTION: Email ----
# ---------------------------------------------------------------------------
def send_email(subject: str, body: str, to: Optional[str] = None) -> None:
    to = to or NOTIFY_TO
    if not GMAIL_USER or not GMAIL_APP_PASSWORD or not to:
        print(f"[email skipped - missing GMAIL_USER/GMAIL_APP_PASSWORD/recipient] {subject}")
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
# All jobs only fire while the process is awake; times are in Asia/Kolkata.
# ---------------------------------------------------------------------------
scheduler = BackgroundScheduler(timezone=TZ)


def job_block_reminder(activity: str, start: str, end: str) -> None:
    send_email(f"Next: {activity}", f"Next: {activity} - {start}-{end}")


def job_daily_review_prompt() -> None:
    link = f"{APP_BASE_URL}/review" if APP_BASE_URL else "/review"
    send_email(
        "Daily review - what actually happened today?",
        f"Fill in what you actually did vs. planned: {link}",
    )


def weekly_block_counts(data: dict, today: date) -> tuple:
    """Count timetable blocks and valid completions for the same seven days."""
    planned_total = completed_total = 0
    logs = data.get("daily_logs", {})
    for offset in range(7):
        day = today - timedelta(days=offset)
        day_str = day.isoformat()
        log = logs.get(day_str, {}) if isinstance(logs, dict) else {}
        if isinstance(log, dict) and "planned_blocks" in log:
            planned = set(log.get("planned_blocks", []))
        else:
            planned = {block["id"] for block in todays_blocks(data, day)}
        completed = set(log.get("completed_blocks", [])) if isinstance(log, dict) else set()
        planned_total += len(planned)
        completed_total += len(planned & completed)
    return planned_total, completed_total


def job_weekly_digest() -> None:
    data = load_data(force=True)
    today = datetime.now(TZ).date()
    lines = ["Weekly digest", ""]

    planned_total, completed_total = weekly_block_counts(data, today)
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
        lines.append("  - none - everything's on pace")

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
                f"{exam['label']} starts in 2 days - {exam_date.strftime('%b %d, %Y')}",
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
        for index, block in enumerate(blocks):
            try:
                start_minutes = _clock_minutes(block["start"])
            except (KeyError, ValueError):
                continue
            reminder_minutes = start_minutes - 5
            reminder_day = day_name
            if reminder_minutes < 0:
                reminder_minutes += 24 * 60
                reminder_day = DAY_NAMES[(DAY_NAMES.index(day_name) - 1) % 7]
            reminder_code = DAY_CODE[reminder_day]
            scheduler.add_job(
                job_block_reminder, "cron", day_of_week=reminder_code,
                hour=reminder_minutes // 60, minute=reminder_minutes % 60,
                args=[block["activity"], block["start"], block["end"]],
                id=f"block_{reminder_code}_{block.get('id', index)}", replace_existing=True,
            )

    scheduler.add_job(job_daily_review_prompt, "cron",
                       hour=DAILY_REVIEW_HOUR, minute=DAILY_REVIEW_MINUTE,
                       id="daily_review", replace_existing=True)
    scheduler.add_job(job_weekly_digest, "cron", day_of_week="sun", hour=21, minute=0,
                       id="weekly_digest", replace_existing=True)
    scheduler.add_job(job_exam_alerts, "cron", hour=8, minute=0,
                       id="exam_alerts", replace_existing=True)
    scheduler.add_job(job_monthly_nudge, "cron", day=1, hour=0, minute=5,
                       id="monthly_nudge", replace_existing=True)


# ---------------------------------------------------------------------------
# ---- SECTION: HTML templates (inline; no templates/ folder) ----
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


def page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    safe_title = html_module.escape(title)
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{safe_title} - Anushasan</title>
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
    return HTMLResponse(html, status_code=status_code)


# ---------------------------------------------------------------------------
# ---- SECTION: Routes ----
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard():
    data = load_data()
    today = datetime.now(TZ).date()
    today_log = data.get("daily_logs", {}).get(today.isoformat(), {})
    todays = todays_blocks(data, today)
    planned_today = [block["id"] for block in todays]
    completed_today = set(today_log.get("completed_blocks", []))
    legacy_ids = {
        f'{block["start"]}-{block["activity"]}': block["id"] for block in todays
    }
    completed_today = {
        legacy_ids.get(block_id, block_id) for block_id in completed_today
    } & set(planned_today)

    blocks_html = ""
    for b in remaining_blocks_today(data):
        blocks_html += (
            f'<div class="check-row"><span class="muted" style="width:5.5rem;">'
            f'{html_module.escape(b["start"])}-{html_module.escape(b["end"])}</span><span>{html_module.escape(b["activity"])}</span></div>'
        )
    if not blocks_html:
        blocks_html = '<p class="muted">Nothing left on today&#39;s timetable.</p>'

    subjects_html = ""
    for subject_name, subject in data.get("subjects", {}).items():
        books = subject.get("books", [])
        pct_values = [max(0, min(100, b["pct"])) for b in books if b.get("pct") is not None]
        avg_pct = round(sum(pct_values) / len(pct_values)) if pct_values else 0
        book_rows = ""
        for b in books:
            rr = run_rate(b, today)
            pct_display = f'{b["pct"]}%' if b.get("pct") is not None else "ongoing"
            flag = (f'<span class="pill">{rr["required_pct_per_week"]}%/wk needed</span>'
                    if rr and rr["behind_pace"] else "")
            book_rows += (
                f'<div class="book-row"><span>{html_module.escape(b["name"])} '
                f'<span class="muted">({pct_display})</span></span>{flag}</div>'
            )
        subjects_html += f"""<div class="card">
            <h3>{html_module.escape(subject_name.replace("_", " "))}</h3>
            <div class="progress-track"><div class="progress-fill" style="width:{avg_pct}%;"></div></div>
            <div class="muted" style="margin-top:0.3rem;">{avg_pct}% average across {len(books)} book(s)</div>
            {book_rows}
        </div>"""

    body = f"""
    <div class="card">
        <h2>Today's review - {today.strftime("%A, %d %B")}</h2>
      <p class="muted">Completed: {len(completed_today)}/{len(planned_today)} planned blocks</p>
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
        safe_subject = html_module.escape(subject_name, quote=True)
        rows = ""
        for i, b in enumerate(subject.get("books", [])):
            if b.get("status") == "ongoing":
                pct_input = '<span class="muted">ongoing</span>'
            else:
                pct_val = b.get("pct") if b.get("pct") is not None else 0
                pct_input = (f'<input type="number" name="pct__{safe_subject}__{i}" '
                             f'min="0" max="100" value="{pct_val}" style="width:5rem;">')
            options = "".join(
                f'<option value="{html_module.escape(s)}" '
                f'{"selected" if b.get("status") == s else ""}>{html_module.escape(s)}</option>'
                for s in statuses
            )
            target_options = '<option value="">No target month</option>' + "".join(
                f'<option value="{month}" '
                f'{"selected" if b.get("target_month") == month else ""}>{month}</option>'
                for month in MONTH_CYCLE
            )
            rows += f"""<div class="book-row">
                <span style="flex:1;min-width:14rem;">
                  <input type="text" name="name__{safe_subject}__{i}"
                         value="{html_module.escape(str(b["name"]), quote=True)}"
                         maxlength="200">
                </span>
                <span style="display:flex;gap:0.5rem;align-items:center;">
                  <select name="status__{safe_subject}__{i}">{options}</select>
                  {pct_input}
                  <select name="target__{safe_subject}__{i}">{target_options}</select>
                </span>
            </div>"""
        books_html += (
            f'<div class="card"><h3>{html_module.escape(subject_name.replace("_", " "))}</h3>{rows}</div>'
        )

    body = f"""
    <form method="post" action="/setup">
      <h2>Update book progress</h2>
      {books_html}
      <button type="submit">Save progress</button>
    </form>
    <div class="card">
      <h2>Advanced: timetable / exam dates / monthly plan</h2>
       <p class="muted">Raw JSON - edit carefully, this replaces those sections wholesale.</p>
      <form method="post" action="/setup/advanced">
        <label>Timetable</label>
        <textarea name="timetable">{html_module.escape(json.dumps(data.get("timetable", {}), indent=2))}</textarea>
        <label>Exam dates</label>
        <textarea name="exam_dates">{html_module.escape(json.dumps(data.get("exam_dates", []), indent=2))}</textarea>
        <label>Monthly plan</label>
        <textarea name="monthly_plan">{html_module.escape(json.dumps(data.get("monthly_plan", {}), indent=2))}</textarea>
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
        prefix, separator, remainder = key.partition("__")
        subject_name, separator, index_text = remainder.rpartition("__")
        if not separator or subject_name not in data["subjects"]:
            continue
        try:
            index = int(index_text)
            book = data["subjects"][subject_name]["books"][index]
        except (ValueError, IndexError, KeyError):
            continue
        value = str(value)
        if prefix == "status" and value in VALID_STATUSES:
            book["status"] = value
            if value == "ongoing":
                book["pct"] = None
            elif book.get("pct") is None:
                book["pct"] = 0
        elif prefix == "name":
            name = value.strip()[:200]
            if name:
                book["name"] = name
        elif prefix == "pct" and value.strip():
            try:
                book["pct"] = max(0, min(100, int(value)))
            except ValueError:
                continue
        elif prefix == "target":
            book["target_month"] = value if value in MONTH_CYCLE else None
    try:
        save_data(data, "Update book progress via /setup")
    except RuntimeError as exc:
        return page("Save conflict", f'<div class="card">{html_module.escape(str(exc))}</div>', 409)
    schedule_all_jobs()
    return RedirectResponse("/setup", status_code=303)


@app.post("/setup/advanced")
async def setup_advanced_submit(request: Request):
    form = await request.form()
    data = load_data()
    try:
        timetable = json.loads(str(form.get("timetable", "")))
        exam_dates = json.loads(str(form.get("exam_dates", "")))
        monthly_plan = json.loads(str(form.get("monthly_plan", "")))
        data["timetable"], data["exam_dates"], data["monthly_plan"] = (
            validate_advanced_sections(timetable, exam_dates, monthly_plan)
        )
    except (json.JSONDecodeError, ValueError, TypeError, KeyError) as exc:
        message = html_module.escape(str(exc))
        return page("Setup - error", f'<div class="card">Invalid settings: {message}. Go back and fix it.</div>')
    try:
        save_data(data, "Update timetable/exam dates/monthly plan via /setup")
    except RuntimeError as exc:
        return page("Save conflict", f'<div class="card">{html_module.escape(str(exc))}</div>', 409)
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
        block_id = b["id"]
        safe_block_id = html_module.escape(block_id, quote=True)
        legacy_id = f'{b["start"]}-{b["activity"]}'
        checked = "checked" if block_id in completed or legacy_id in completed else ""
        rows += f"""<div class="check-row">
            <input type="checkbox" name="completed" value="{safe_block_id}" {checked} id="{safe_block_id}">
            <label for="{safe_block_id}" style="margin:0;">{html_module.escape(b["start"])}-{html_module.escape(b["end"])} - {html_module.escape(b["activity"])}</label>
        </div>"""

    study_rows = ""
    for subject_name, subject in data.get("subjects", {}).items():
        book_rows = ""
        for index, book in enumerate(subject.get("books", [])):
            safe_subject = html_module.escape(subject_name, quote=True)
            safe_name = html_module.escape(str(book["name"]))
            if book.get("chapter_total"):
                control = (
                    f'<input type="number" name="chapters__{safe_subject}__{index}" '
                    f'min="0" max="{book["chapter_total"]}" value="0" style="width:5rem;"> '
                    f'<span class="muted">chapters today '
                    f'(total {book["chapter_total"]}, done {book.get("chapters_completed", 0)})</span>'
                )
            elif book.get("status") != "ongoing":
                control = (
                    f'<input type="number" name="progress__{safe_subject}__{index}" '
                    f'min="0" max="100" placeholder="{book.get("pct", 0)}" style="width:5rem;"> '
                    '<span class="muted">% complete</span>'
                )
            else:
                control = '<span class="muted">ongoing - no percentage</span>'
            book_rows += (
                f'<div class="book-row"><span>{safe_name}</span>'
                f'<span style="display:flex;gap:0.4rem;align-items:center;">{control}</span></div>'
            )
        if book_rows:
            study_rows += (
                f'<div class="card"><h3>{html_module.escape(subject_name.replace("_", " "))}</h3>'
                f'{book_rows}</div>'
            )

    body = f"""
    <form method="post" action="/review">
      <div class="card">
        <h2>Today's review - {today.strftime("%A, %d %B")}</h2>
        <p class="muted">Check off what you actually did.</p>
        {rows}
        <label>Notes</label>
        <textarea name="notes" style="min-height:80px;font-family:inherit;">{html_module.escape(str(existing.get("notes", "")))}</textarea>
      </div>
      <div class="card">
        <h2>Study progress</h2>
        <p class="muted">For chapter books, enter chapters completed today. Other books use current percentage.</p>
        {study_rows}
      </div>
      <button type="submit">Save review</button>
    </form>
    """
    return page("Review", body)


@app.post("/review")
async def review_submit(request: Request):
    form = await request.form()
    data = load_data()
    today_str = datetime.now(TZ).date().isoformat()
    blocks = todays_blocks(data)
    planned_ids = [b["id"] for b in blocks]
    completed_ids = list(dict.fromkeys(
        value for value in form.getlist("completed") if value in planned_ids
    ))
    study_entries = []
    for key, raw_value in form.items():
        prefix, separator, remainder = key.partition("__")
        subject_name, separator, index_text = remainder.rpartition("__")
        if not separator or prefix not in {"chapters", "progress"}:
            continue
        try:
            index = int(index_text)
            book = data["subjects"][subject_name]["books"][index]
            amount = int(str(raw_value))
        except (ValueError, TypeError, KeyError, IndexError):
            continue
        if amount < 0:
            continue
        if prefix == "chapters" and book.get("chapter_total"):
            remaining = book["chapter_total"] - book.get("chapters_completed", 0)
            added = min(amount, max(remaining, 0))
            book["chapters_completed"] = book.get("chapters_completed", 0) + added
            book["pct"] = round(100 * book["chapters_completed"] / book["chapter_total"])
            if book["pct"] >= 100:
                book["status"] = "done"
            elif added and book["status"] == "not_started":
                book["status"] = "in_progress"
            study_entries.append({
                "subject": subject_name, "book": book["name"],
                "chapters": added, "type": "chapters",
            })
        elif prefix == "progress" and not book.get("chapter_total"):
            book["pct"] = max(0, min(100, amount))
            if book["pct"] >= 100:
                book["status"] = "done"
            elif book["pct"] and book["status"] == "not_started":
                book["status"] = "in_progress"
            study_entries.append({
                "subject": subject_name, "book": book["name"],
                "pct": book["pct"], "type": "percentage",
            })
    data.setdefault("daily_logs", {})[today_str] = {
        "planned_blocks": planned_ids,
        "completed_blocks": completed_ids,
        "notes": str(form.get("notes", ""))[:5000],
        "study_entries": study_entries,
    }
    try:
        save_data(data, f"Daily review for {today_str}")
    except RuntimeError as exc:
        return page("Save conflict", f'<div class="card">{html_module.escape(str(exc))}</div>', 409)
    return RedirectResponse("/review", status_code=303)


@app.get("/test-email")
def test_email():
    send_email("Anushasan test email", "If you're reading this, email sending works.")
    ok = bool(GMAIL_USER and GMAIL_APP_PASSWORD)
    return {"status": "sent" if ok else "skipped - missing GMAIL_USER/GMAIL_APP_PASSWORD"}


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
