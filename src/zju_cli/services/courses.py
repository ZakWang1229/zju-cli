from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from ..adapters.courses import CoursesAdapter
from ..adapters.homework import HomeworkAdapter
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
        return CoursesAdapter(session).json(session, path, method=method, body=body)

    def _paged(self, session, path, key):
        return CoursesAdapter(session).paged(session, path, key)

    def todos(self):
        return self._read(lambda session: CoursesAdapter(session).todos())

    def courses(self, keyword="", statuses=None):
        statuses = statuses or ["ongoing", "notStarted", "ended"]
        if not set(statuses) <= {"ongoing", "notStarted", "ended"}:
            raise InputError("课程状态无效")
        return self._read(lambda session: CoursesAdapter(session).courses(keyword, statuses))

    def _user_id(self, session, course_id):
        return HomeworkAdapter(session)._user_id(session, course_id)

    def _submission(self, session, activity_id, user_id):
        return HomeworkAdapter(session)._submission(session, activity_id, user_id)

    def homework(self, course_id, include_submission=True):
        course_id = identifier(course_id)
        return self._read(lambda session: HomeworkAdapter(session).homework(course_id, include_submission))

    _submission_summary = staticmethod(HomeworkAdapter._submission_summary)

    def submit_homework(self, activity_id, file_path, comment=""):
        activity_id = identifier(activity_id)
        source = Path(file_path).expanduser().absolute()
        try:
            content = source.read_bytes()
        except OSError as exc:
            raise LocalFileError(f"无法读取提交文件：{source}") from exc
        session = self._session()
        self.on_progress("预留上传文件 …")
        adapter = HomeworkAdapter(session)
        reserve = adapter.reserve_upload(source.name, len(content))
        self.on_progress(f"上传 {source.name}（{len(content)} bytes） …")
        adapter.upload_content(reserve, source.name, content, self.settings.timeout_seconds)
        self.on_progress("提交作业 …")
        try:
            _, submitted = adapter.submit(activity_id, reserve["id"], comment)
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
            activity = HomeworkAdapter(session).activity(activity_id)
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
            _, result = HomeworkAdapter(session).retract(submission["id"])
        except (RemoteError, SessionExpired) as exc:
            raise RemoteError("撤回请求未获得成功确认；请先查询作业状态") from exc
        return {"ok": True, "activity_id": activity_id, "submission_id": submission["id"],
                "message": result.get("message") or "已撤回为草稿"}
