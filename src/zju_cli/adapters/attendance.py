"""Only platform transport and response interpretation; no scheduling or guesses."""
import json
import re
import uuid
from urllib.parse import urlsplit

from ..clients.auth import COURSES
from ..errors import RemoteError, SessionExpired
from ..validation import identifier, required_list

SUCCESS = {"on_call", "on_call_fine"}


def completed(value):
    return any(value.get(key) in SUCCESS for key in ("status", "status_name"))


class AttendanceAdapter:
    def __init__(self, session):
        self.session = session

    def activities(self):
        _, value = self.session.json(COURSES + "/api/radar/rollcalls")
        return required_list(value, "rollcalls")

    def details(self, activity_id):
        _, value = self.session.json(COURSES + f"/api/rollcall/{identifier(activity_id)}/student_rollcalls")
        return value

    def code(self, activity_id):
        value = self.details(activity_id)
        code = value.get("number_code")
        if type(code) is int and 1000 <= code <= 9999:
            return str(code)
        # Keep leading zeros; never infer, enumerate or manufacture a code.
        return code if isinstance(code, str) and re.fullmatch(r"[0-9]{4}", code) else None

    def confirmed(self, activity_id):
        rows = self.activities()
        return any(str(row.get("rollcall_id")) == str(activity_id) and completed(row) for row in rows)

    def _submit(self, path, payload):
        status, raw, final = self.session.request(COURSES + path, method="PUT",
            data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"},
            allowed_statuses=(400, 409, 422))
        if "/cas/login" in urlsplit(final).path:
            raise SessionExpired("签到会话已失效，结果需核实")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise RemoteError("签到响应无法解析，结果需核实") from exc
        if not isinstance(value, dict):
            raise RemoteError("签到响应格式异常，结果需核实")
        # HTTP 200 alone never means that attendance succeeded.
        return {"confirmed": status == 200 and completed(value),
                "rejected": bool(value.get("error_code")),
                "wrong_code": value.get("error_code") == "wrong_number_code",
                "out_of_scope": value.get("error_code") == "radar_out_of_scope",
                "distance": value.get("distance", (value.get("data") or {}).get("distance",
                    (value.get("result") or {}).get("distance")))}

    def number(self, activity_id, code):
        return self._submit(f"/api/rollcall/{identifier(activity_id)}/answer_number_rollcall",
            {"deviceId": str(uuid.uuid4()), "numberCode": code})

    def radar(self, activity_id, point):
        return self._submit(f"/api/rollcall/{identifier(activity_id)}/answer?api_version=1.1.2",
            {"deviceId": str(uuid.uuid4()), "longitude": point[0], "latitude": point[1],
             "speed": None, "accuracy": 68, "altitude": None, "altitudeAccuracy": None, "heading": None})
