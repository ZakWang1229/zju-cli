from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from .courses import CoursesAdapter
from ..clients.auth import COURSES
from ..clients.http import HttpSession
from ..errors import InputError, LocalFileError, RemoteError, SessionExpired
from ..validation import identifier, required_list

SHANGHAI = timezone(timedelta(hours=8))
TODO_TYPES = {"homework": "作业", "exam": "考试", "quiz": "测验", "survey": "问卷"}


def shanghai_date(value) -> str | None:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, str) and not value.isdigit():
            instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=timezone.utc)
        else:
            instant = datetime.fromtimestamp(float(value), timezone.utc)
        return instant.astimezone(SHANGHAI).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


class HomeworkAdapter(CoursesAdapter):
    def reserve_upload(self, name, size):
        status, reserve = self.json(self.session, "/api/uploads", method="POST", body={
            "name": name, "size": size, "parent_type": "submission_upload", "parent_id": 0,
            "is_scorm": False, "embed_material_type": ""})
        if status != 201 or not reserve.get("id") or not reserve.get("upload_url"):
            raise RemoteError("占位上传失败或返回字段不完整")
        return reserve

    def upload_content(self, reserve, name, content, timeout):
        url = reserve["upload_url"]
        if urlsplit(url).scheme != "https":
            raise RemoteError("上传地址不是 HTTPS")
        boundary = "zju-" + uuid.uuid4().hex
        filename = name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        multipart = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                     'Content-Type: application/octet-stream\r\n\r\n').encode("utf-8")
        multipart += content + f"\r\n--{boundary}--\r\n".encode()
        HttpSession(timeout).request(url, method="PUT", data=multipart,
            headers={"Content-Type": "multipart/form-data; boundary=" + boundary})

    def submit(self, activity_id, upload_id, comment):
        return self.json(self.session, f"/api/course/activities/{activity_id}/submissions",
            method="POST", body={"uploads": [upload_id], "comment": comment, "slides": [], "is_draft": False})

    def activity(self, activity_id):
        return self.json(self.session, f"/api/activities/{activity_id}")[1]

    def retract(self, submission_id):
        return self.json(self.session, f"/api/submissions/{submission_id}/retract", method="PUT", body={})

    def _json(self, *args, **kwargs):
        return self.json(*args, **kwargs)

    def _paged(self, *args, **kwargs):
        return self.paged(*args, **kwargs)

    def _read(self, operation):
        return operation(self.session)

    def _user_id(self, session, course_id):
        _, data = self._json(session, f"/api/course/{course_id}/activity-reads-for-user")
        rows = required_list(data, "activity_reads")
        user_id = (rows[0].get("created_for_id") or rows[0].get("created_by_id")) if rows else None
        if not user_id:
            raise RemoteError("无法解析当前用户 ID")
        return user_id

    def _submission(self, session, activity_id, user_id):
        _, data = self._json(session, f"/api/activities/{activity_id}/students/{user_id}/submission_list")
        rows = required_list(data, "list")
        return rows[0] if rows else None

    def homework(self, course_id, include_submission=True):
        course_id = identifier(course_id)
        def fetch(session):
            activities = self._paged(session, f"/api/courses/{course_id}/homework-activities", "homework_activities")
            warnings = []
            user_id = None
            if include_submission and activities:
                try:
                    user_id = self._user_id(session, course_id)
                except SessionExpired:
                    raise
                except RemoteError:
                    warnings.append("当前用户 ID 无法解析，未获取提交详情")
            homeworks = []
            for activity in activities:
                item = {"activity_id": activity.get("id"), "title": activity.get("title") or "",
                        "type": TODO_TYPES.get(activity.get("type"), activity.get("type") or ""),
                        "deadline": shanghai_date(activity.get("end_time") or activity.get("deadline")),
                        "end_time_utc": activity.get("end_time") or activity.get("deadline"),
                        "is_ended": bool(activity.get("is_closed")), "in_progress": bool(activity.get("is_in_progress")),
                        "submitted": bool(activity.get("submitted")),
                        "allow_retract": bool((activity.get("data") or {}).get("allow_retract")),
                        "weight_percent": activity.get("score_percentage"), "score": None}
                if user_id:
                    try:
                        submission = self._submission(session, activity["id"], user_id)
                        if submission:
                            final_score = submission.get("final_score")
                            item["score"] = final_score if final_score is not None else submission.get("score")
                            item["submission"] = self._submission_summary(submission)
                    except SessionExpired:
                        raise
                    except RemoteError:
                        warnings.append(f"作业 {activity.get('id')} 的提交详情获取失败")
                homeworks.append(item)
            result = {"course_id": int(course_id) if course_id.isdigit() else course_id,
                      "count": len(homeworks), "homeworks": homeworks}
            if warnings:
                result["warnings"] = warnings
            return result
        return self._read(fetch)

    @staticmethod
    def _submission_summary(submission):
        return {"submission_id": submission.get("id"), "is_draft": submission.get("is_draft"),
                "can_retract": submission.get("can_retract"), "submitted_at": submission.get("submitted_at"),
                "files": [u.get("name") for u in submission.get("uploads") or []],
                "score": submission.get("score"), "final_score": submission.get("final_score"),
                "rubric_score": submission.get("rubric_score"), "graded_at": submission.get("score_at"),
                "instructor_comment": submission.get("instructor_comment") or ""}
