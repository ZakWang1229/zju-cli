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


class CoursesAdapter:
    def __init__(self, session):
        self.session = session

    def json(self, session, path, *, method="GET", body=None):
        return session.json(COURSES + path, method=method, body=body,
                            headers={"X-Requested-With": "XMLHttpRequest"})

    def paged(self, session, path, key):
        result = []
        for page in range(1, 1001):
            separator = "&" if "?" in path else "?"
            _, data = self.json(session, f"{path}{separator}page={page}&page_size=50")
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
            _, data = self.json(session, "/api/todos?no-intercept=true")
            key = "todo_list" if "todo_list" in data else "todoList"
            todos = [{"id": row.get("id"), "course_name": row.get("course_name") or "",
                      "course_code": row.get("course_code") or "", "title": row.get("title") or "",
                      "type": TODO_TYPES.get(row.get("type"), row.get("type") or "待办"),
                      "deadline": shanghai_date(row.get("end_time")), "end_time_utc": row.get("end_time")}
                     for row in required_list(data, key)]
            todos.sort(key=lambda row: row["deadline"] or "9999-12-31")
            return {"count": len(todos), "todos": todos}
        return fetch(self.session)

    def courses(self, keyword="", statuses=None):
        statuses = statuses or ["ongoing", "notStarted", "ended"]
        if not set(statuses) <= {"ongoing", "notStarted", "ended"}:
            raise InputError("课程状态无效")
        condition = json.dumps({"status": statuses, "keyword": keyword or ""}, ensure_ascii=False, separators=(",", ":"))
        def fetch(session):
            rows = self.paged(session, "/api/my-courses?conditions=" + quote(condition, safe=""), "courses")
            courses = [{"id": row.get("id"), "name": row.get("name") or "",
                        "course_code": row.get("course_code") or "",
                        "academic_year": (row.get("academic_year") or {}).get("name", ""),
                        "semester": (row.get("semester") or {}).get("name", ""),
                        "department": (row.get("department") or {}).get("name", ""),
                        "teachers": "、".join(t.get("name") or "" for t in row.get("instructors") or []),
                        "class": (row.get("course_attributes") or {}).get("teaching_class_name") or "",
                        "study_completeness": row.get("study_completeness")} for row in rows]
            return {"count": len(courses), "courses": courses}
        return fetch(self.session)
