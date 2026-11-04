import os
import queue
import subprocess
import tempfile
import time
import threading
import weakref
from pathlib import Path

from zju_cli.shared.errors import CancelledError, LocalFileError, SessionExpired
from zju_cli.shared.files import output_path
from zju_cli.shared.audio import export_audio
from zju_cli.backend.modules.tasks import current_task


class ClassroomModule:
    def __init__(self, daemon, config):
        self.daemon = daemon
        self.config = config
        self._lock = threading.Lock()
        self._session = None
        self._local = threading.local()
        self._sessions = weakref.WeakSet()
        self.service = ClassroomService(config, read=self._read, on_progress=daemon.report_progress)
        daemon.command_registry.register("schedule", self.schedule)
        daemon.command_registry.register("classroom.schedule", self.schedule)
        daemon.command_registry.register(
            "classroom.audio.download", self.audio_download,
            mode="job", durable=True, resumable=True, version=1)
        daemon.command_registry.register(
            "classroom.audio.export", self.audio_export, durable=True)

    def _cmc_session(self, *, refresh=False):
        context = current_task.get()
        timeout = context.http_timeout if context and hasattr(context, "http_timeout") else self.config.timeout_seconds
        if getattr(self._local, "session", None) is None or refresh or self._local.session.timeout != timeout:
            session = self.daemon.zjuam.cmc(timeout)
            self._local.session = session
            with self._lock:
                self._sessions.add(session)
        session = self._local.session
        return session, session.user_id

    def _read(self, operation):
        try:
            return operation(self._cmc_session())
        except SessionExpired:
            return operation(self._cmc_session(refresh=True))

    def schedule(self, start_date=None, end_date=None):
        return self.service.schedule(start_date, end_date)

    def playback_url(self, sub_id, course_id, session=None):
        if session is not None:
            from zju_cli.shared.validation import identifier
            cmc = session if isinstance(session, tuple) else (session, getattr(session, "user_id", None))
            return playback_url(cmc, identifier(sub_id), identifier(course_id))
        return self.service.playback_url(sub_id, course_id)

    def audio_download(self, sub_id, course_id, out, *, overwrite=False,
                       ffmpeg_bin=None, timeout=None):
        context = self.daemon.task
        target = Path(out).expanduser().absolute()
        source = context.artifact_path("audio" + target.suffix.lower())
        metadata = (context.recovery or {}).get("audio")
        if metadata and (not isinstance(metadata, dict) or type(metadata.get("bytes")) is not int
                         or metadata["bytes"] <= 0 or not source.is_file() or source.stat().st_size != metadata["bytes"]):
            metadata = None
        if metadata is None:
            target = output_path(out, overwrite)
            context.report(stage="authenticating")
            metadata = self.service.audio(sub_id, course_id, source, overwrite=True,
                ffmpeg_bin=ffmpeg_bin, timeout=timeout,
                on_media_progress=lambda progress: context.report(stage="extracting", progress=progress),
                should_stop=context.should_stop, register_process=context.register_process,
                unregister_process=context.unregister_process)
            metadata["written"] = True
            context.checkpoint({"audio": metadata})
        context.report(stage="writing")
        context.check()
        from zju_cli.shared.errors import LocalFileError
        try:
            exported = export_audio(source, target, overwrite=overwrite, check_stop=context.check)
            return {**metadata, "out_path": str(exported), "written": True}
        except LocalFileError:
            return {**metadata, "out_path": str(source), "written": True,
                    "requested_output_path": str(target), "output_error": "导出冲突或失败，完整音频产物已保留"}

    def audio_export(self, source_task_id, out, overwrite=False):
        from zju_cli.shared.errors import InputError, LocalFileError
        task = self.daemon.tasks.get(source_task_id)
        if task["state"] != "succeeded" or task["command"] != "classroom.audio.download":
            raise InputError("来源任务没有可导出的完整音频")
        metadata = task["result"]
        suffix = Path(task["parameters"]["out"]).suffix.lower()
        artifact_dir = task.get("artifact_dir") or (
            Path(task.get("artifact_root") or self.daemon.state_dir) / "tasks" / source_task_id)
        source = Path(artifact_dir) / ("audio" + suffix)
        if not source.is_file() or source.stat().st_size != metadata["bytes"]:
            raise LocalFileError("任务产物不完整")
        if Path(out).suffix.lower() != suffix:
            raise InputError("导出必须保持音频容器扩展名")
        exported = export_audio(source, out, overwrite=overwrite, check_stop=self.daemon.task.check_cancelled)
        result = {**metadata, "out_path": str(exported), "written": True}
        result.pop("output_error", None)
        result.pop("requested_output_path", None)
        return result

    def close(self):
        with self._lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        for session in sessions:
            session.close()



import json
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit

from zju_cli.shared.errors import InputError, RemoteError
from zju_cli.shared.files import output_path, staged_output
from zju_cli.shared.validation import identifier, required_list, validate_audio_timeout

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
              on_media_progress=None, should_stop=None, register_process=None,
              unregister_process=None):
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
                          on_media_progress=on_media_progress, should_stop=should_stop,
                          register_process=register_process, unregister_process=unregister_process)
            size = temporary.stat().st_size
        return {"ok": True, "sub_id": sub_id, "course_id": course_id, "out_path": str(target), "bytes": size}


def schedule(cmc, start, end):
    session, user_id = cmc
    query = urlencode({
        "user_id": user_id,
        "tenant_id": "112",
        "start_at": start.isoformat(),
        "end_at": end.isoformat(),
    })
    _, data = session.json(
        YJAPI + "/courseapi/v2/schedule/get-week-schedules?" + query)
    result = data.get("result")
    if not isinstance(result, dict):
        raise RemoteError("课表接口未返回有效 result")
    courses = []
    for day in required_list(result, "list"):
        for row in required_list(day, "course"):
            courses.append({
                "sub_id": row.get("id"),
                "course_id": row.get("course_id"),
                "course_title": row.get("course_title") or "",
                "teacher_name": row.get("teacher_name") or "",
                "room_name": row.get("room_name") or "",
                "status_label": row.get("status_label") or "",
                "start_at": shanghai_time(row.get("start_at")),
                "end_at": shanghai_time(row.get("end_at")),
            })
    return {
        "range": str(start) if start == end else f"{start}~{end}",
        "count": len(courses),
        "courses": courses,
    }


def playback_url(cmc, sub_id, course_id):
    session, _ = cmc
    _, data = session.json(
        YJAPI
        + "/courseapi/v2/course/catalogue?"
        + urlencode({"course_id": course_id})
    )
    result = data.get("result")
    payload = result.get("data") if isinstance(result, dict) else None
    items = payload.get("item") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise RemoteError("回放目录接口格式异常")
    item = next(
        (
            row
            for row in items
            if isinstance(row, dict) and str(row.get("sub_id")) == sub_id
        ),
        None,
    )
    if item is None:
        raise RemoteError("未找到该节课的回放记录")
    content = item.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError as exc:
            raise RemoteError("回放内容格式异常") from exc
    if not isinstance(content, dict):
        raise RemoteError("该节课尚无回放内容")
    url = (
        (content.get("save_playback") or {}).get("contents")
        or (content.get("playback") or {}).get("url")
    )
    if not isinstance(url, str) or urlsplit(url).scheme not in {"http", "https"}:
        raise RemoteError("该节课尚无有效回放链接")
    return url


AUDIO_FORMATS = {".m4a", ".aac", ".mp4", ".mka", ".ogg", ".mp3", ".wav"}


def extract_audio(
        url: str, target: Path, *, binary="ffmpeg", timeout=None,
        on_progress=None, on_media_progress=None, should_stop=None,
        register_process=None, unregister_process=None):
    validate_audio_timeout(timeout)
    notify = on_progress or (lambda message: None)
    arguments = [
        binary,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-i",
        url,
        "-map",
        "0:a:0",
        "-vn",
        "-c:a",
        "copy",
        "-progress",
        "pipe:1",
        "-nostats",
        str(target),
    ]
    with tempfile.TemporaryFile() as error_log:
        try:
            process_options = (
                {"creationflags": subprocess.CREATE_NO_WINDOW}
                if os.name == "nt" else {"start_new_session": True}
            )
            process = subprocess.Popen(
                arguments,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=error_log,
                text=True,
                encoding="utf-8",
                errors="replace",
                **process_options,
            )
        except OSError as exc:
            raise LocalFileError(
                f"无法启动 ffmpeg：{binary}；请检查安装和 PATH") from exc
        events = queue.Queue()

        def read_progress():
            for line in process.stdout:
                key, separator, value = line.strip().partition("=")
                if separator and key == "out_time":
                    events.put(value)

        reader = threading.Thread(target=read_progress, daemon=True)
        reader.start()
        started = last_report = time.monotonic()
        position = "00:00:00"
        try:
            if register_process:
                register_process(process)
            while process.poll() is None:
                if should_stop and should_stop():
                    raise CancelledError("音频提取已取消")
                try:
                    position = events.get(timeout=0.5)
                except queue.Empty:
                    pass
                now = time.monotonic()
                if on_media_progress:
                    on_media_progress({
                        "elapsed_seconds": int(now - started),
                        "media_position": position,
                    })
                if timeout is not None and now - started >= timeout:
                    raise LocalFileError("音频提取超过 --timeout，已停止 ffmpeg")
                if now - last_report >= 5:
                    notify(
                        f"音频提取运行中，已用 {int(now - started)} 秒；"
                        f"媒体位置 {position}"
                    )
                    last_report = now
            if process.returncode != 0:
                error_log.seek(0, 2)
                size = error_log.tell()
                error_log.seek(max(0, size - 1500))
                diagnostic = error_log.read().decode("utf-8", errors="replace")
                diagnostic = re.sub(r"https?://\S+", "[media URL]", diagnostic)
                raise LocalFileError(
                    f"ffmpeg 失败（退出码 {process.returncode}）："
                    f"{diagnostic.strip()}"
                )
            if not target.exists() or target.stat().st_size == 0:
                raise LocalFileError("ffmpeg 未生成有效音频文件")
            if should_stop and should_stop():
                raise CancelledError("音频提取已取消")
        finally:
            from zju_cli.backend.modules.tasks import stop_process
            stop_process(process)
            reader.join(timeout=5)
            process.stdout.close()
            if unregister_process:
                unregister_process(process)
