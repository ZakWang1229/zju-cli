import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from zju_cli.shared.config import Settings
from zju_cli.shared.errors import InputError, LocalFileError, RemoteError, SessionExpired
from zju_cli.shared.files import staged_output
from zju_cli.backend.modules.courses import CoursesService, shanghai_date
from zju_cli.backend.modules.classroom import ClassroomService


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        settings = Settings("student", "secret")
        self.courses = CoursesService(settings, read=lambda operation: operation(self.session),
                                      session=lambda: self.session, on_progress=lambda message: None)
        self.classroom = ClassroomService(settings, read=lambda operation: operation((self.session, "user-1")),
                                          on_progress=lambda message: None)

    def test_deadline_uses_shanghai_timezone(self):
        self.assertEqual(shanghai_date("2026-10-03T20:00:00Z"), "2026-10-04")
        self.assertEqual(shanghai_date("2026-10-04T01:00:00+08:00"), "2026-10-04")
        self.assertIsNone(shanghai_date("invalid"))

    def test_platform_read_errors_propagate_to_module_boundary(self):
        self.session.json.side_effect = RemoteError("timeout")
        with self.assertRaises(RemoteError):
            self.courses.todos()

    def test_session_expiry_is_visible_to_module_for_one_refresh(self):
        self.session.json.side_effect = SessionExpired("expired")
        with self.assertRaises(SessionExpired):
            self.courses.todos()

    def test_courses_pagination_keeps_all_pages(self):
        self.session.json.side_effect = [(200, {"courses": [{"id": 1}], "pages": 2}),
                                         (200, {"courses": [{"id": 2}], "pages": 2})]
        value = self.courses.courses("数学", ["ongoing"])
        self.assertEqual([row["id"] for row in value["courses"]], [1, 2])
        query = parse_qs(urlsplit(self.session.json.call_args_list[0].args[0]).query)
        self.assertEqual(json.loads(query["conditions"][0])["keyword"], "数学")

    def test_invalid_date_fails_before_login(self):
        for start, end in [("20261004", None), ("2026-02-30", None), ("2026-10-04", "2026-10-11"),
                           ("2026-10-04", "2026-10-03")]:
            with self.assertRaises(InputError):
                self.classroom.schedule(start, end)

    def test_api_error_is_not_an_empty_schedule(self):
        self.session.json.return_value = (200, {"code": 500, "msg": "error"})
        with self.assertRaises(RemoteError):
            self.classroom.schedule("2026-10-04")

    def test_homework_score_falls_back_when_final_null(self):
        self.session.json.side_effect = [
            (200, {"homework_activities": [{"id": 42}], "pages": 1}),
            (200, {"activity_reads": [{"created_for_id": 7}]}),
            (200, {"list": [{"id": 100, "final_score": None, "score": 90}]}),
        ]
        self.assertEqual(self.courses.homework("1")["homeworks"][0]["score"], 90)

    def test_schedule_keeps_replay_metadata_without_transcript_field(self):
        self.session.json.return_value = (200, {"result": {"list": [{"course": [
            {"id": 42, "course_id": 1, "course_title": "lesson", "status_label": "回放",
             "start_at": 0, "end_at": 60}]}]}})
        result = self.classroom.schedule("1970-01-01")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["courses"][0], {
            "sub_id": 42, "course_id": 1, "course_title": "lesson", "teacher_name": "",
            "room_name": "", "status_label": "回放", "start_at": "1970-01-01 08:00",
            "end_at": "1970-01-01 08:01"})

    def test_invalid_ids_fail_before_login(self):
        with self.assertRaises(InputError):
            self.courses.homework("../course")

    def test_publish_refuses_file_created_during_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "lesson.md"
            with self.assertRaises(LocalFileError):
                with staged_output(target, False) as temporary:
                    temporary.write_text("new")
                    target.write_text("original")
            self.assertEqual(target.read_text(), "original")
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_submit_has_exactly_one_post_and_confirms(self):
        self.session.json.side_effect = [(201, {"id": 10, "created_by_id": 7, "upload_url": "https://upload.example/file?token=secret"}),
                                         (200, {"message": "done"}), (200, {"list": [{"id": 100, "is_draft": False}]})]
        with tempfile.TemporaryDirectory() as directory, patch("zju_cli.backend.modules.courses.HttpSession") as uploader:
            source = Path(directory) / "answer.txt"
            source.write_text("answer", encoding="utf-8")
            result = self.courses.submit_homework("42", source)
            self.assertTrue(result["ok"])
            self.assertEqual(result["submission"]["submission_id"], 100)
            upload_call = uploader.return_value.request.call_args
            self.assertEqual(upload_call.kwargs["method"], "PUT")
            self.assertIn(b'answer\r\n--zju-', upload_call.kwargs["data"])
        posts = [call for call in self.session.json.call_args_list if call.kwargs.get("method") == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[1].kwargs["body"]["uploads"], [10])

    def test_submit_timeout_never_replays_write(self):
        self.session.json.side_effect = [(201, {"id": 10, "upload_url": "https://upload.example/file"}), RemoteError("timeout")]
        with tempfile.TemporaryDirectory() as directory, patch("zju_cli.backend.modules.courses.HttpSession"):
            source = Path(directory) / "answer.txt"
            source.write_text("answer")
            with self.assertRaisesRegex(RemoteError, "结果可能已生效"):
                self.courses.submit_homework("42", source)
        self.assertEqual(self.session.json.call_count, 2)

    def test_retract_permission_refusal_sends_no_put(self):
        self.session.json.side_effect = [(200, {"course_id": 1, "data": {"allow_retract": False}}),
                                         (200, {"activity_reads": [{"created_for_id": 7}]}),
                                         (200, {"list": [{"id": 100, "is_draft": False}]})]
        self.assertFalse(self.courses.retract_homework("42")["ok"])
        self.assertTrue(all(call.kwargs.get("method") == "GET" for call in self.session.json.call_args_list))

    def test_retract_success(self):
        self.session.json.side_effect = [(200, {"course_id": 1, "data": {"allow_retract": True}}),
                                         (200, {"activity_reads": [{"created_for_id": 7}]}),
                                         (200, {"list": [{"id": 100, "can_retract": True}]}), (200, {})]
        self.assertTrue(self.courses.retract_homework("42")["ok"])
        self.assertEqual(self.session.json.call_args.kwargs["method"], "PUT")

    def test_failed_audio_preserves_existing_target(self):
        self.session.json.return_value = (200, {"result": {"data": {"item": [
            {"sub_id": "42", "content": json.dumps({"save_playback": {"contents": "https://video.example/file.mp4"}})}]}}})
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "lesson.m4a"
            target.write_bytes(b"notes")
            with patch("zju_cli.backend.modules.classroom.extract_audio", side_effect=LocalFileError("ffmpeg failed")):
                with self.assertRaises(LocalFileError):
                    self.classroom.audio("42", "1", target, overwrite=True)
            self.assertEqual(target.read_bytes(), b"notes")
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_invalid_audio_timeout_fails_before_login(self):
        for value in (0, -1, float("nan")):
            with self.assertRaises(InputError):
                self.classroom.audio("42", "1", "lesson.m4a", timeout=value)
