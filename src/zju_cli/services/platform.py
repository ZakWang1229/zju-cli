from __future__ import annotations

import time

from ..clients.auth import CasAuth
from ..config import Settings
from ..errors import SessionExpired
from .classroom import ClassroomService
from .courses import CoursesService


class ZjuService:
    def __init__(self, settings: Settings, *, auth=None, on_progress=None):
        self.settings = settings
        self.auth = auth or CasAuth(settings)
        self.on_progress = on_progress or (lambda message: None)
        self._sessions = {}
        self._courses = CoursesService(
            settings, read=lambda operation: self._read("courses", operation),
            session=lambda: self._session("courses"), on_progress=self.on_progress)
        self._classroom = ClassroomService(
            settings, read=lambda operation: self._read("cmc", operation), on_progress=self.on_progress)

    def _session(self, platform: str, *, force=False):
        cached = self._sessions.get(platform)
        if not force and cached and time.monotonic() - cached[1] < self.settings.session_ttl_seconds:
            return cached[0]
        self.on_progress(f"登录{'学在浙大' if platform == 'courses' else '智云课堂'} …")
        session = getattr(self.auth, platform)()
        self._sessions[platform] = (session, time.monotonic())
        return session

    def _read(self, platform, operation):
        session = self._session(platform)
        try:
            return operation(session)
        except SessionExpired:
            self.on_progress("会话失效，重新登录并重试一次")
            return operation(self._session(platform, force=True))

    def todos(self):
        return self._courses.todos()

    def courses(self, keyword="", statuses=None):
        return self._courses.courses(keyword, statuses)

    def homework(self, course_id, include_submission=True):
        return self._courses.homework(course_id, include_submission)

    def submit_homework(self, activity_id, file_path, comment=""):
        return self._courses.submit_homework(activity_id, file_path, comment)

    def retract_homework(self, activity_id):
        return self._courses.retract_homework(activity_id)

    def schedule(self, start_date=None, end_date=None):
        return self._classroom.schedule(start_date, end_date)

    def audio(self, sub_id, course_id, out_path, *, overwrite=False, ffmpeg_bin=None, timeout=None,
              on_media_progress=None, should_stop=None):
        return self._classroom.audio(
            sub_id, course_id, out_path, overwrite=overwrite, ffmpeg_bin=ffmpeg_bin, timeout=timeout,
            on_media_progress=on_media_progress, should_stop=should_stop)
