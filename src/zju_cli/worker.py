from __future__ import annotations

import argparse
import re
import sys
import threading
import time
import uuid
from pathlib import Path

from .config import Settings
from .errors import BusyError, CancelledError, ConfigError, LocalFileError, ZjuError
from .services.audio_jobs import export_audio
from .services.platform import ZjuService
from .storage import JobStore


class Worker:
    def __init__(self, store, job_id, *, service=None, token=None):
        self.store, self.job_id = store, job_id
        self.service = service
        self.token = token or uuid.uuid4().hex
        self.reserved = token is not None
        self.stop_heartbeat = threading.Event()
        self.lease_lost = threading.Event()

    def update(self, changes):
        self.store.update(self.job_id, changes, token=self.token)

    def heartbeat(self):
        while not self.stop_heartbeat.wait(10):
            try:
                self.store.heartbeat(self.job_id, self.token)
            except ZjuError:
                self.lease_lost.set()
                return

    def should_stop(self):
        if self.lease_lost.is_set():
            raise BusyError("Worker 租约失效，已停止提取")
        return self.store.get(self.job_id).get("cancel_requested", False)

    def check_stop(self):
        if self.should_stop():
            raise CancelledError("音频提取已取消")

    def progress(self, value):
        self.check_stop()
        self.update({"stage": "extracting", "progress": value})

    def message(self, text):
        self.check_stop()
        # Media URLs may contain authentication parameters; keep only human progress.
        self.update({"message": re.sub(r"https?://\S+", "[media URL]", text)})

    def execute(self):
        job = self.store.get(self.job_id)
        self.check_stop()
        self.update({"status": "running", "stage": "authenticating", "started_at": time.time()})
        source = self.store.job_dir(self.job_id) / ("audio" + Path(job["output_path"]).suffix.lower())
        if not job.get("result"):
            if self.service is None:
                config_path = Path(job["config_path"]) if job.get("config_path") else None
                settings = Settings.load(config_path, job["http_timeout"], load_cwd=False)
                if settings.username != job["username"]:
                    raise ConfigError("当前配置与任务提交账号不同，恢复原配置后 resume")
                self.service = ZjuService(settings, on_progress=self.message)
            self.update({"stage": "extracting"})
            metadata = self.service.audio(job["sub_id"], job["course_id"], source, overwrite=True,
                ffmpeg_bin=job["ffmpeg_bin"], timeout=job["timeout"],
                on_media_progress=self.progress, should_stop=self.should_stop)
            self.check_stop()
            metadata["written"] = True
            self.update({"stage": "writing", "result": metadata, "progress": None})
        else:
            metadata = dict(job["result"])
        self.check_stop()
        try:
            if source.stat().st_size != metadata["bytes"] or metadata["bytes"] == 0:
                raise LocalFileError("内部音频结果不完整")
        except OSError as exc:
            raise LocalFileError("内部音频结果不可读") from exc
        try:
            export_audio(source, Path(job["output_path"]), overwrite=job["overwrite"], check_stop=self.check_stop)
        except ZjuError as exc:
            if isinstance(exc, (CancelledError, BusyError)):
                raise
            metadata.update({"output_error": str(exc), "requested_output_path": job["output_path"]})
        else:
            metadata.update({"out_path": job["output_path"]})
        self.update({"status": "succeeded", "stage": "finished", "result": metadata,
                     "completed_at": time.time(), "progress": None, "error": None, "error_exit_code": None})

    def run(self):
        if not self.reserved:
            self.store.reserve(self.job_id, self.token)
        self.store.adopt(self.job_id, self.token)
        thread = threading.Thread(target=self.heartbeat, daemon=True)
        thread.start()
        try:
            try:
                self.execute()
            except BusyError:
                return
            except ZjuError as exc:
                status = "cancelled" if isinstance(exc, CancelledError) else (
                    "paused" if isinstance(exc, ConfigError) else "failed")
                self.update({"status": status, "stage": "finished" if status != "paused" else "authenticating",
                             "error": re.sub(r"https?://\S+", "[media URL]", str(exc)),
                             "error_exit_code": exc.exit_code,
                             "completed_at": time.time() if status != "paused" else None})
            except Exception as exc:
                self.update({"status": "paused", "error": "Worker 内部错误；检查 worker.log 后 resume",
                             "error_type": type(exc).__name__})
                print(f"Worker internal error: {type(exc).__name__}", file=sys.stderr)
        finally:
            self.stop_heartbeat.set()
            thread.join(timeout=5)
            self.store.release(self.job_id, self.token)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("job_id")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--lease-token")
    args = parser.parse_args(argv)
    try:
        Worker(JobStore(args.data_dir), args.job_id, token=args.lease_token).run()
        return 0
    except ZjuError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
