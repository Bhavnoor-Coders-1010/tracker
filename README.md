# Anushasan tracker

Single-file FastAPI UPSC/academics tracker. Data is stored in `data.json` in
the repository named by `GITHUB_DATA_REPO`.

## Run locally

```bash
pip install -r requirements.txt
uvicorn app:app --reload
```

Without GitHub credentials the app uses `seed_data.json` in memory, which is
useful for local UI checks. For persistence set `GITHUB_TOKEN` (a token with
Contents read/write access) and `GITHUB_DATA_REPO` (`owner/repo`). Email
delivery uses Resend over HTTPS: set `RESEND_API_KEY`, `EMAIL_FROM`
(`onboarding@resend.dev` for testing), and `NOTIFY_TO`.

The `/setup` page accepts validated JSON for the timetable, exam dates, and
monthly plan. Timetable blocks use stable `id` values; omitted IDs are
generated automatically. Times are interpreted in `Asia/Kolkata`. The daily
review reminder runs at **23:45 IST**, weekly digest at Sunday 21:00 IST.

On `/review`, chapter-tracked books ask for chapters completed that day; the
app accumulates those chapters and derives the book percentage from the stored
total. Other non-ongoing books accept their current percentage directly.

Run the focused checks with:

```bash
python -m unittest discover -s tests -v
```
