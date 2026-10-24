from __future__ import annotations
import json
from urllib.parse import urlencode, urlsplit
from ..errors import RemoteError
from ..validation import required_list
from datetime import datetime, timedelta, timezone
SHANGHAI = timezone(timedelta(hours=8))
def shanghai_time(value) -> str | None:
    if value is None or value == "":
        return None
    try:
        return datetime.fromtimestamp(float(value), SHANGHAI).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError, OverflowError):
        return None



YJAPI = "https://yjapi.cmc.zju.edu.cn"

def schedule(cmc, start, end):
    session, user_id = cmc
    query = urlencode({"user_id": user_id, "tenant_id": "112", "start_at": start.isoformat(), "end_at": end.isoformat()})
    _, data = session.json(YJAPI + "/courseapi/v2/schedule/get-week-schedules?" + query)
    result = data.get("result")
    if not isinstance(result, dict):
        raise RemoteError("课表接口未返回有效 result")
    courses = []
    for day in required_list(result, "list"):
        for row in required_list(day, "course"):
            courses.append({"sub_id": row.get("id"), "course_id": row.get("course_id"),
                            "course_title": row.get("course_title") or "",
                            "teacher_name": row.get("teacher_name") or "", "room_name": row.get("room_name") or "",
                            "status_label": row.get("status_label") or "",
                            "start_at": shanghai_time(row.get("start_at")), "end_at": shanghai_time(row.get("end_at"))})
    return {"range": str(start) if start == end else f"{start}~{end}", "count": len(courses), "courses": courses}
