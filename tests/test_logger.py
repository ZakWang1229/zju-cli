import logging
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from zju_cli.backend.logger import configure_logging


class LoggerTests(unittest.TestCase):
    def tearDown(self):
        logger = logging.getLogger("zju_cli")
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)

    def test_daily_file_name_timezone_and_append(self):
        with tempfile.TemporaryDirectory() as temporary:
            logger = configure_logging(log_dir=temporary)
            record = logger.makeRecord("zju_cli.test", logging.INFO, "", 1, "hello", (), None)
            record.created = datetime(2026, 11, 3, 17, 0, tzinfo=timezone.utc).timestamp()
            logger.handlers[0].emit(record)
            logger.handlers[0].close()
            path = Path(temporary) / "daemon-2026-11-04.log"
            self.assertIn("2026-11-04T01:00:00.000+08:00", path.read_text(encoding="utf-8"))
            logger = configure_logging(log_dir=temporary)
            record = logger.makeRecord("zju_cli.test", logging.INFO, "", 1, "again", (), None)
            record.created = datetime(2026, 11, 3, 17, 0, tzinfo=timezone.utc).timestamp()
            logger.handlers[0].emit(record)
            handler = logger.handlers[0]
            handler.close()
            logger.removeHandler(handler)
            self.assertIn("again", path.read_text(encoding="utf-8"))

    def test_stderr_output(self):
        logger = configure_logging(output="stderr")
        self.assertIsInstance(logger.handlers[0], logging.StreamHandler)

    def test_day_rollover_and_disk_failure_retry(self):
        from zju_cli.backend.logger import DailyFileHandler
        with tempfile.TemporaryDirectory() as root:
            handler = DailyFileHandler(root)
            record = logging.LogRecord("zju_cli.test", logging.INFO, "", 1, "safe message", (), None)
            record.created = datetime(2026, 11, 3, 15, 59, tzinfo=timezone.utc).timestamp()
            handler.emit(record)
            record.created += 120
            handler.emit(record)
            self.assertEqual(len(list(Path(root).glob("*.log"))), 2)
            handler.close_stream()
            handler._date = datetime.fromtimestamp(record.created, handler.timezone).date()
            handler._stream = Mock()
            handler._stream.write.side_effect = OSError("private error detail")
            with patch("zju_cli.backend.logger.sys.stderr") as errors:
                handler.emit(record)
                self.assertNotIn("private", str(errors.write.call_args))
            handler.emit(record)
            handler.close()
            self.assertIn("safe message", (Path(root) / "daemon-2026-11-04.log").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
