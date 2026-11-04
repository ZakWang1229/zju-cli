"""Daemon logger, formatter, and daily file handler."""
from datetime import datetime
import logging
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from zju_cli.shared.storage import default_data_dir


class DailyFileHandler(logging.Handler):
    def __init__(self, directory, *, timezone="Asia/Shanghai"):
        super().__init__()
        self.directory = Path(directory)
        self.timezone = ZoneInfo(timezone)
        self._date = None
        self._stream = None

    def emit(self, record):
        try:
            now = datetime.fromtimestamp(record.created, self.timezone)
            current = now.date()
            if current != self._date:
                self.close_stream()
                self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                self._stream = (self.directory / f"daemon-{current.isoformat()}.log").open(
                    "a", encoding="utf-8", newline="\n")
                if os.name != "nt":
                    self.directory.chmod(0o700)
                    os.chmod(self._stream.name, 0o600)
                self._date = current
            stamp = now.isoformat(timespec="milliseconds")
            self._stream.write(f"{stamp} {record.levelname} {record.name}: {self.format(record)}\n")
            self._stream.flush()
        except Exception:
            self._date = None
            try:
                self.close_stream()
            except OSError:
                pass
            try:
                sys.stderr.write("zju-cli: diagnostic log write failed\n")
            except OSError:
                pass

    def close_stream(self):
        stream, self._stream = self._stream, None
        self._date = None
        if stream is not None:
            try:
                stream.flush()
            finally:
                stream.close()

    def close(self):
        self.acquire()
        try:
            try:
                self.close_stream()
            except OSError:
                pass
            super().close()
        finally:
            self.release()


class ZonedFormatter(logging.Formatter):
    def __init__(self, timezone):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.zone = ZoneInfo(timezone)

    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, self.zone).isoformat(timespec="milliseconds")


def configure_logging(*, level="INFO", output="file", log_dir=None, timezone="Asia/Shanghai"):
    logger = logging.getLogger("zju_cli")
    logger.setLevel(getattr(logging, level.upper()))
    logger.propagate = False
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)
    if output == "stderr":
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(ZonedFormatter(timezone))
    elif output == "file":
        directory = Path(log_dir) if log_dir else default_data_dir() / "logs"
        handler = DailyFileHandler(directory, timezone=timezone)
    else:
        raise ValueError("log output must be file or stderr")
    logger.addHandler(handler)
    return logger
