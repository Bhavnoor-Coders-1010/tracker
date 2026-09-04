import base64
import unittest
from datetime import date
from unittest.mock import Mock, patch

import app


class TrackerDataTests(unittest.TestCase):
    def test_normalize_data_clamps_progress_and_adds_stable_block_ids(self):
        raw = {
            "subjects": {"<History>": {"books": [
                {"name": "<book>", "status": "invalid", "pct": 140}
            ]}},
            "timetable": {"Monday": [
                {"start": "23:30", "end": "01:00", "activity": "<late>"}
            ]},
        }
        first = app.normalize_data(raw)
        second = app.normalize_data(raw)
        self.assertEqual(first["subjects"]["<History>"]["books"][0]["pct"], 100)
        self.assertEqual(first["timetable"]["Monday"][0]["id"],
                         second["timetable"]["Monday"][0]["id"])
        self.assertEqual(first["timetable"]["Monday"][0]["id"][0], "b")

    def test_chapter_book_progress_is_derived_from_chapters(self):
        data = app.normalize_data({
            "subjects": {"History": {"books": [{
                "name": "Modern History - Spectrum",
                "status": "not_started",
                "pct": 0,
                "chapters_completed": 3,
            }]}},
        })
        book = data["subjects"]["History"]["books"][0]
        self.assertEqual(book["chapter_total"], 39)
        self.assertEqual(book["chapters_completed"], 3)
        self.assertEqual(book["pct"], 8)

    def test_weekly_counts_ignore_duplicate_and_stray_completions(self):
        data = {"daily_logs": {
            "2026-09-03": {
                "planned_blocks": ["a", "a", "b"],
                "completed_blocks": ["a", "stray", "a"],
            }
        }}
        self.assertEqual(app.weekly_block_counts(data, date(2026, 9, 4)), (2, 1))

    def test_advanced_validation_rejects_invalid_clock(self):
        with self.assertRaises(ValueError):
            app.validate_advanced_sections(
                {"Monday": [{"start": "25:00", "end": "26:00", "activity": "x"}]},
                [], {},
            )

    def test_advanced_validation_rejects_overlapping_blocks(self):
        with self.assertRaises(ValueError):
            app.validate_advanced_sections(
                {"Monday": [
                    {"start": "09:00", "end": "10:00", "activity": "a"},
                    {"start": "09:30", "end": "11:00", "activity": "b"},
                ]},
                [], {},
            )

    def test_headers_use_bearer_token(self):
        old = app.GITHUB_TOKEN
        try:
            app.GITHUB_TOKEN = "test-token"
            self.assertEqual(
                app._headers()["Authorization"], "Bearer " + app.GITHUB_TOKEN
            )
        finally:
            app.GITHUB_TOKEN = old

    def test_send_email_uses_resend_api(self):
        old = app.RESEND_API_KEY, app.EMAIL_FROM, app.NOTIFY_TO
        try:
            app.RESEND_API_KEY = "test-key"
            app.EMAIL_FROM = "onboarding@resend.dev"
            app.NOTIFY_TO = "me@example.com"
            response = Mock(status_code=200)
            with patch.object(app.requests, "post", return_value=response) as post:
                app.send_email("Subject", "Body")
            self.assertEqual(post.call_args.kwargs["json"], {
                "from": "onboarding@resend.dev",
                "to": ["me@example.com"],
                "subject": "Subject",
                "text": "Body",
            })
            self.assertEqual(
                post.call_args.kwargs["headers"]["Authorization"], "Bearer test-key"
            )
        finally:
            app.RESEND_API_KEY, app.EMAIL_FROM, app.NOTIFY_TO = old

    def test_github_conflict_refreshes_sha_once(self):
        old_token, old_repo, old_sha = (
            app.GITHUB_TOKEN, app.GITHUB_DATA_REPO, app._sha_cache
        )
        try:
            app.GITHUB_TOKEN = "test-token"
            app.GITHUB_DATA_REPO = "owner/data"
            app._sha_cache = "old-sha"
            conflict = Mock(status_code=409)
            updated = Mock(status_code=200)
            updated.json.return_value = {"content": {"sha": "new-sha"}}
            latest = Mock(status_code=200)
            latest.json.return_value = {
                "sha": "new-sha",
                "content": base64.b64encode(b"{}").decode("ascii"),
            }
            with patch.object(app.requests, "put", side_effect=[conflict, updated]) as put, \
                    patch.object(app.requests, "get", return_value=latest):
                app._github_write({}, "test")
            self.assertEqual(app._sha_cache, "new-sha")
            self.assertEqual(put.call_args_list[0].kwargs["json"]["sha"], "old-sha")
            self.assertEqual(put.call_args_list[1].kwargs["json"]["sha"], "new-sha")
        finally:
            app.GITHUB_TOKEN, app.GITHUB_DATA_REPO, app._sha_cache = (
                old_token, old_repo, old_sha
            )

    def test_review_escapes_user_content(self):
        old_loader = app.load_data
        try:
            day = app.DAY_NAMES[app.datetime.now(app.TZ).weekday()]
            app.load_data = lambda: app.normalize_data({
                "subjects": {},
                "timetable": {day: [{
                    "start": "00:00", "end": "01:00", "activity": "<script>"
                }]},
                "daily_logs": {},
            })
            response = app.review_form()
            self.assertIn("&lt;script&gt;", response.body.decode("utf-8"))
            self.assertNotIn("<script>", response.body.decode("utf-8"))
        finally:
            app.load_data = old_loader


if __name__ == "__main__":
    unittest.main()
