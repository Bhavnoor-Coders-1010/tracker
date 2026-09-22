# Anushasan tracker

Single-file FastAPI UPSC/academics tracker. Data is stored in `data.json` in
the repository named by `GITHUB_DATA_REPO`.

## Run locally

```bash
pip install -r requirements.txt
uvicorn app:app --reload
```

The app loads configuration from a local `.env` file automatically. Copy the
provided `.env` template, fill in the blank values, and keep that file private.
It is excluded from Git by `.gitignore`.

Without GitHub credentials the app uses `seed_data.json` in memory, which is
useful for local UI checks. For persistence set `GITHUB_TOKEN` (a token with
Contents read/write access) and `GITHUB_DATA_REPO` (`owner/repo`). Email
delivery uses Resend over HTTPS: set `RESEND_API_KEY`, `EMAIL_FROM`
(`onboarding@resend.dev` for testing), and `NOTIFY_TO`.

For current-affairs audio reminders, configure these additional environment
variables:

```text
GEMINI_API_KEY
TELEGRAM_API_ID
TELEGRAM_API_HASH
TELEGRAM_CHAT
SMTP_HOST
SMTP_PORT                 # usually 587
SMTP_USER
SMTP_PASSWORD
CAF_AUDIO_ATTACHED        # true by default
```

The Telegram chat should contain PDFs whose names match `TELEGRAM_FILE_REGEX`
(by default, `CURRENT AFFAIRS*.pdf`). Five minutes before a timetable block
whose activity starts with `Current Affairs`, the app checks for a new matching
PDF, summarizes it with Gemini, generates a WAV briefing, and sends it as an
email attachment through SMTP. The normal reminder email continues to use
Resend. Optional overrides include `GEMINI_TEXT_MODEL`, `GEMINI_TTS_MODEL`,
`GEMINI_TTS_VOICE`, and `TELEGRAM_FILE_REGEX`.

### Telegram authentication on Render

Do not perform the phone-number login from Render; Render cannot answer an
interactive OTP prompt. Authenticate once on your local computer:

```bash
python telegram_session_setup.py
```

The command asks for the phone number, Telegram login code, and two-factor
password if enabled. Copy the printed `TELEGRAM_SESSION_STRING` into the
Render environment variables. Also set `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`,
and `TELEGRAM_CHAT` there. The deployed app then reuses that authenticated
session without asking for the phone number or OTP during normal restarts or
deploys.

Treat `TELEGRAM_SESSION_STRING` like a password. Never commit it or paste it
into public logs. Telegram may require a new local login if the session is
revoked, the account is logged out, or Telegram invalidates it.

The `/setup` page accepts validated JSON for the timetable, exam dates, and
monthly plan. Timetable blocks use stable `id` values; omitted IDs are
generated automatically. Times are interpreted in `Asia/Kolkata`. The daily
review reminder runs at **23:45 IST**, weekly digest at Sunday 21:00 IST.

Timetable reminders use a short format with the upcoming time block, one
random motivational line, and a direct call to action. UPSC and current-affairs
activities use UPSC-specific lines; other activities use general motivation.
The reminder is scheduled **five minutes before** the timetable block starts,
not at the block's start time. For deployment diagnostics, `/scheduler-status`
shows the scheduler timezone, running state, and next run times without
exposing credentials.

On `/review`, chapter-tracked books ask for chapters completed that day; the
app accumulates those chapters and derives the book percentage from the stored
total. Other non-ongoing books accept their current percentage directly.

Run the focused checks with:

```bash
python -m unittest discover -s tests -v
```
