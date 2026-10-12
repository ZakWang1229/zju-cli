from __future__ import annotations

import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .errors import CancelledError, LocalFileError
from .validation import validate_audio_timeout

AUDIO_FORMATS = {".m4a", ".aac", ".mp4", ".mka", ".ogg", ".mp3", ".wav"}


def extract_audio(url: str, target: Path, *, binary="ffmpeg", timeout=None, on_progress=None,
                  on_media_progress=None, should_stop=None):
    validate_audio_timeout(timeout)
    notify = on_progress or (lambda message: None)
    arguments = [binary, "-nostdin", "-hide_banner", "-loglevel", "warning", "-y",
                 "-i", url, "-map", "0:a:0", "-vn", "-c:a", "copy",
                 "-progress", "pipe:1", "-nostats", str(target)]
    with tempfile.TemporaryFile() as error_log:
        try:
            # A detached worker has no console for ffmpeg to inherit on Windows.
            process_options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=error_log, text=True, encoding="utf-8", errors="replace",
                                       **process_options)
        except OSError as exc:
            raise LocalFileError(f"无法启动 ffmpeg：{binary}；请检查安装和 PATH") from exc
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
            while process.poll() is None:
                if should_stop and should_stop():
                    raise CancelledError("音频提取已取消")
                try:
                    position = events.get(timeout=0.5)
                except queue.Empty:
                    pass
                now = time.monotonic()
                if on_media_progress:
                    on_media_progress({"elapsed_seconds": int(now - started), "media_position": position})
                if timeout is not None and now - started >= timeout:
                    raise LocalFileError("音频提取超过 --timeout，已停止 ffmpeg")
                if now - last_report >= 5:
                    notify(f"音频提取运行中，已用 {int(now - started)} 秒；媒体位置 {position}")
                    last_report = now
            if process.returncode != 0:
                error_log.seek(0, 2)
                size = error_log.tell()
                error_log.seek(max(0, size - 1500))
                diagnostic = error_log.read().decode("utf-8", errors="replace")
                diagnostic = re.sub(r"https?://\S+", "[media URL]", diagnostic)
                raise LocalFileError(f"ffmpeg 失败（退出码 {process.returncode}）：{diagnostic.strip()}")
            if not target.exists() or target.stat().st_size == 0:
                raise LocalFileError("ffmpeg 未生成有效音频文件")
            if should_stop and should_stop():
                raise CancelledError("音频提取已取消")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            reader.join(timeout=5)
            process.stdout.close()
