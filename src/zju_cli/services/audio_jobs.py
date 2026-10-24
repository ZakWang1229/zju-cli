from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from ..config import Settings
from ..errors import BusyError, InputError, LocalFileError
from ..files import output_path, staged_output
from ..media import AUDIO_FORMATS
from ..storage import JobStore
from ..validation import identifier, validate_audio_timeout


def summary(job):
    keys = ("job_id", "status", "stage", "sub_id", "course_id", "output_path", "worker_active",
            "created_at", "updated_at", "completed_at", "attempts", "progress", "message",
            "error", "error_exit_code", "cancel_requested", "result")
    result = {key: job[key] for key in keys if key in job}
    result["elapsed_seconds"] = max(0, int((job.get("completed_at") or time.time()) - job["created_at"]))
    return result


def export_audio(source, target, *, overwrite=False, check_stop=None):
    target = output_path(target, overwrite)
    with staged_output(target, overwrite) as temporary:
        shutil.copyfile(source, temporary)
        if check_stop:
            check_stop()
    return target


class AudioJobService:
    def __init__(self, store: JobStore, *, create=None):
        self.store = store
        self.create = create or store.create

    def submit(self, sub_id, course_id, out, *, settings, config_path=None, overwrite=False,
               ffmpeg_bin=None, timeout=None, launch=True):
        sub_id, course_id = identifier(sub_id), identifier(course_id)
        validate_audio_timeout(timeout)
        target = output_path(out, overwrite)
        if target.suffix.lower() not in AUDIO_FORMATS:
            raise InputError("音频输出需要有效扩展名，建议使用 .m4a")
        binary = ffmpeg_bin or settings.ffmpeg_bin
        if os.path.dirname(binary):
            binary = str(Path(binary).expanduser().absolute())
        effective_config = Path(config_path).expanduser().absolute() if config_path is not None else None
        if effective_config is None and (Path.cwd() / ".env").is_file():
            effective_config = Path.cwd() / ".env"
        job_id, now = uuid.uuid4().hex, time.time()
        document = {"job_id": job_id, "kind": "class-audio", "task_type": "class-audio", "task_version": 1,
                    "status": "queued", "stage": "queued",
                    "created_at": now, "updated_at": now, "sub_id": sub_id, "course_id": course_id,
                    "output_path": str(target), "overwrite": overwrite, "ffmpeg_bin": binary, "timeout": timeout,
                    "config_path": str(effective_config) if effective_config else None,
                    "username": settings.username, "http_timeout": settings.timeout_seconds,
                    "cancel_requested": False, "attempts": 0}
        document["parameters"] = {key: document[key] for key in (
            "sub_id", "course_id", "output_path", "overwrite", "ffmpeg_bin", "timeout", "http_timeout")}
        self.create(document)
        if launch:
            self.launch(job_id)
        return summary(self.store.get(job_id))

    def launch(self, job_id, *, config_path=None):
        from ..session_daemon import DaemonClient
        job = self.store.get(job_id)
        effective_config = Path(config_path) if config_path is not None else (
            Path(job["config_path"]) if job.get("config_path") else None)
        settings = Settings.load(effective_config, job["http_timeout"], load_cwd=False)
        if settings.username != job["username"]:
            raise InputError("恢复配置与原任务账号不同")
        DaemonClient(settings).call({"action": "job_resume", "data_dir": str(self.store.root.resolve()),
                                    "job_id": job_id, "username": settings.username})

    def _launch_legacy(self, job_id, *, config_path=None):
        # Compatibility helper for exercising an existing Worker installation.
        # New submissions and resumes always use launch(), the unified daemon.
        token = uuid.uuid4().hex
        self.store.reserve(job_id, token, changes={"config_path": str(config_path)} if config_path is not None else None)
        command = [sys.executable, "-m", "zju_cli.worker", job_id,
                   "--data-dir", str(self.store.root), "--lease-token", token]
        kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "close_fds": True,
                  "cwd": str(self.store.root)}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        try:
            with (self.store.job_dir(job_id) / "worker.log").open("ab") as logfile:
                process = subprocess.Popen(command, stderr=logfile, **kwargs)
            self.store.update(job_id, {"launched_pid": process.pid})
        except OSError as exc:
            try:
                self.store.update(job_id, {"status": "paused", "error": "无法启动 Worker；可使用 class-audio jobs resume"}, token=token)
            finally:
                self.store.release(job_id, token)
            raise LocalFileError(f"后台进程无法启动；job_id={job_id}") from exc

    def resume(self, job_id, *, config_path=None):
        job = self.store.get(job_id)
        if job["worker_active"]:
            raise BusyError("已有 Worker 运行中；崩溃后租约最多需 60 秒过期")
        if job["status"] in {"succeeded", "cancelled"}:
            raise InputError("任务已结束；成功结果使用 jobs result")
        effective_config = Path(config_path).expanduser().absolute() if config_path is not None else (
            Path(job["config_path"]) if job.get("config_path") else None)
        settings = Settings.load(effective_config, job["http_timeout"], load_cwd=False)
        if settings.username != job["username"]:
            raise InputError("恢复配置与原任务账号不同")
        self.launch(job_id, config_path=effective_config if config_path is not None else None)
        return summary(self.store.get(job_id))

    def result(self, job_id, *, out=None, overwrite=False):
        job = self.store.get(job_id)
        if job["status"] != "succeeded":
            raise InputError(f"结果尚不可用，任务状态：{job['status']}")
        metadata = dict(job["result"])
        source = self.store.job_dir(job_id) / ("audio" + Path(job["output_path"]).suffix.lower())
        try:
            if source.stat().st_size != metadata["bytes"] or metadata["bytes"] == 0:
                raise LocalFileError("任务音频结果不完整")
        except OSError as exc:
            raise LocalFileError("任务音频结果无法读取") from exc
        if out is not None:
            target = Path(out).expanduser().absolute()
            if target.suffix.lower() != source.suffix.lower():
                raise InputError("导出必须保持音频容器扩展名；不会自动转码")
            if target.resolve() != source.resolve():
                export_audio(source, target, overwrite=overwrite)
            metadata.update({"out_path": str(target), "written": True})
            metadata.pop("output_error", None)
            metadata.pop("requested_output_path", None)
            self.store.update(job_id, {"result": metadata})
        elif not Path(metadata["out_path"]).is_file():
            metadata.update({"out_path": str(source), "output_error": "外部音频文件已不存在；内部结果保留"})
        return {"job_id": job_id, **metadata}
