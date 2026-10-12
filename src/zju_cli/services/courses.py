from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

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


class CoursesService:
    def __init__(self, settings, *, read, session, on_progress):
        self.settings = settings
        self._read = read
        self._session = session
        self.on_progress = on_progress

    def _json(self, session, path, *, method="GET", body=None):
        return session.json(COURSES + path, method=method, body=body,
                            headers={"X-Requested-With": "XMLHttpRequest"})

    def _paged(self, session, path, key):
        result = []
        for page in range(1, 1001):
            separator = "&" if "?" in path else "?"
            _, data = self._json(session, f"{path}{separator}page={page}&page_size=50")
            rows = required_list(data, key)
            result.extend(rows)
            try:
                pages = int(data.get("pages") or 1)
            except (TypeError, ValueError) as exc:
                raise RemoteError("列表接口页数格式异常") from exc
            if not rows or page >= pages:
                return result
        raise RemoteError("分页超过 1000 页，已停止读取")

    def todos(self):
        def fetch(session):
            _, data = self._json(session, "/api/todos?no-intercept=true")
            key = "todo_list" if "todo_list" in data else "todoList"
            todos = [{"id": row.get("id"), "course_name": row.get("course_name") or "",
                      "course_code": row.get("course_code") or "", "title": row.get("title") or "",
                      "type": TODO_TYPES.get(row.get("type"), row.get("type") or "待办"),
                      "deadline": shanghai_date(row.get("end_time")), "end_time_utc": row.get("end_time")}
                     for row in required_list(data, key)]
            todos.sort(key=lambda row: row["deadline"] or "9999-12-31")
            return {"count": len(todos), "todos": todos}
        return self._read(fetch)

    def courses(self, keyword="", statuses=None):
        statuses = statuses or ["ongoing", "notStarted", "ended"]
        if not set(statuses) <= {"ongoing", "notStarted", "ended"}:
            raise InputError("课程状态无效")
        condition = json.dumps({"status": statuses, "keyword": keyword or ""}, ensure_ascii=False, separators=(",", ":"))
        def fetch(session):
            rows = self._paged(session, "/api/my-courses?conditions=" + quote(condition, safe=""), "courses")
            courses = [{"id": row.get("id"), "name": row.get("name") or "",
                        "course_code": row.get("course_code") or "",
                        "academic_year": (row.get("academic_year") or {}).get("name", ""),
                        "semester": (row.get("semester") or {}).get("name", ""),
                        "department": (row.get("department") or {}).get("name", ""),
                        "teachers": "、".join(t.get("name") or "" for t in row.get("instructors") or []),
                        "class": (row.get("course_attributes") or {}).get("teaching_class_name") or "",
                        "study_completeness": row.get("study_completeness")} for row in rows]
            return {"count": len(courses), "courses": courses}
        return self._read(fetch)

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

    def submit_homework(self, activity_id, file_path, comment=""):
        activity_id = identifier(activity_id)
        source = Path(file_path).expanduser().absolute()
        try:
            content = source.read_bytes()
        except OSError as exc:
            raise LocalFileError(f"无法读取提交文件：{source}") from exc
        session = self._session()
        self.on_progress("预留上传文件 …")
        status, reserve = self._json(session, "/api/uploads", method="POST", body={
            "name": source.name, "size": len(content), "parent_type": "submission_upload",
            "parent_id": 0, "is_scorm": False, "embed_material_type": "",
        })
        if status != 201 or not reserve.get("id") or not reserve.get("upload_url"):
            raise RemoteError("占位上传失败或返回字段不完整")
        upload_url = reserve["upload_url"]
        if urlsplit(upload_url).scheme != "https":
            raise RemoteError("上传地址不是 HTTPS")
        boundary = "zju-" + uuid.uuid4().hex
        filename = source.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        multipart = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                     'Content-Type: application/octet-stream\r\n\r\n').encode("utf-8")
        multipart += content + f"\r\n--{boundary}--\r\n".encode()
        self.on_progress(f"上传 {source.name}（{len(content)} bytes） …")
        HttpSession(self.settings.timeout_seconds).request(upload_url, method="PUT", data=multipart,
            headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
        self.on_progress("提交作业 …")
        try:
            _, submitted = self._json(session, f"/api/course/activities/{activity_id}/submissions",
                method="POST", body={"uploads": [reserve["id"]], "comment": comment, "slides": [], "is_draft": False})
        except (RemoteError, SessionExpired) as exc:
            raise RemoteError("提交请求未获得成功确认，结果可能已生效；请先查询作业状态，避免重复提交") from exc
        result = {"ok": True, "activity_id": activity_id, "file": source.name,
                  "upload_id": reserve["id"], "message": submitted.get("message") or "已提交"}
        try:
            user_id = reserve.get("created_by_id")
            if not user_id:
                raise RemoteError("上传结果缺少用户 ID")
            submission = self._submission(session, activity_id, user_id)
            if not submission:
                raise RemoteError("提交列表为空")
            result["submission"] = self._submission_summary(submission)
        except (RemoteError, SessionExpired):
            result["warnings"] = ["提交接口已成功，但读回确认失败；请查询作业状态"]
        return result

    def retract_homework(self, activity_id):
        activity_id = identifier(activity_id)
        def fetch(session):
            _, activity = self._json(session, f"/api/activities/{activity_id}")
            course_id = activity.get("course_id")
            if not course_id:
                raise RemoteError("作业接口未返回课程 ID")
            user_id = self._user_id(session, course_id)
            return session, activity, self._submission(session, activity_id, user_id)
        session, activity, submission = self._read(fetch)
        if not submission:
            return {"ok": False, "activity_id": activity_id, "message": "该作业没有提交记录"}
        if submission.get("is_draft"):
            return {"ok": True, "activity_id": activity_id, "submission_id": submission["id"], "message": "当前已是草稿"}
        if submission.get("can_retract") is False or (activity.get("data") or {}).get("allow_retract") is False:
            return {"ok": False, "activity_id": activity_id, "submission_id": submission["id"], "message": "该作业不允许撤回"}
        try:
            _, result = self._json(session, f"/api/submissions/{submission['id']}/retract", method="PUT", body={})
        except (RemoteError, SessionExpired) as exc:
            raise RemoteError("撤回请求未获得成功确认；请先查询作业状态") from exc
        return {"ok": True, "activity_id": activity_id, "submission_id": submission["id"],
                "message": result.get("message") or "已撤回为草稿"}
