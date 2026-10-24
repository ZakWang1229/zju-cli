from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit

from ..adapters.schedule import schedule
from ..adapters.playback import playback_url
from ..errors import InputError, RemoteError
from ..files import output_path, staged_output
from ..media import AUDIO_FORMATS, extract_audio
from ..validation import identifier, required_list, validate_audio_timeout

YJAPI = "https://yjapi.cmc.zju.edu.cn"
SHANGHAI = timezone(timedelta(hours=8))


def shanghai_time(value) -> str | None:
    if value is None or value == "":
        return None
    try:
        return datetime.fromtimestamp(float(value), SHANGHAI).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError, OverflowError):
        return None


class ClassroomService:
    def __init__(self, settings, *, read, on_progress):
        self.settings = settings
        self._read = read
        self.on_progress = on_progress

    def schedule(self, start_date=None, end_date=None):
        try:
            for value in (start_date, end_date):
                if value is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise ValueError
            start = date.fromisoformat(start_date) if start_date else datetime.now(SHANGHAI).date()
            end = date.fromisoformat(end_date) if end_date else start
        except ValueError as exc:
            raise InputError("日期需为有效的 YYYY-MM-DD 格式") from exc
        if not 0 <= (end - start).days <= 6:
            raise InputError("end_date 不能早于 date，且单次最多查询 7 天")
        return self._read(lambda cmc: schedule(cmc, start, end))

    def playback_url(self, sub_id, course_id):
        sub_id, course_id = identifier(sub_id), identifier(course_id)
        return self._read(lambda cmc: playback_url(cmc, sub_id, course_id))

    def audio(self, sub_id, course_id, out_path, *, overwrite=False, ffmpeg_bin=None, timeout=None,
              on_media_progress=None, should_stop=None):
        sub_id, course_id = identifier(sub_id), identifier(course_id)
        validate_audio_timeout(timeout)
        target = output_path(out_path, overwrite)
        if target.suffix.lower() not in AUDIO_FORMATS:
            raise InputError("音频输出需要有效扩展名，建议使用 .m4a")
        video_url = self.playback_url(sub_id, course_id)
        self.on_progress("提取回放音频 …")
        with staged_output(target, overwrite) as temporary:
            extract_audio(video_url, temporary, binary=ffmpeg_bin or self.settings.ffmpeg_bin,
                          timeout=timeout, on_progress=self.on_progress,
                          on_media_progress=on_media_progress, should_stop=should_stop)
            size = temporary.stat().st_size
        return {"ok": True, "sub_id": sub_id, "course_id": course_id, "out_path": str(target), "bytes": size}
