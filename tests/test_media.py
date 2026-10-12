import io
import os
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

from zju_cli.errors import CancelledError, LocalFileError
from zju_cli.media import extract_audio


class MediaTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_real_ffmpeg_copies_synthetic_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.wav"
            target = Path(directory) / "output.wav"
            with wave.open(str(source), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(8000)
                audio.writeframes(b"\x00\x00" * 8000)
            extract_audio(str(source), target, timeout=10)
            with wave.open(str(target), "rb") as audio:
                self.assertEqual(audio.getnframes(), 8000)

    def test_timeout_stops_child_process(self):
        process = Mock()
        process.stdout = io.StringIO()
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as directory, patch("zju_cli.media.subprocess.Popen", return_value=process), \
             patch("zju_cli.media.time.monotonic", side_effect=[0, 2]):
            with self.assertRaisesRegex(LocalFileError, "超过"):
                extract_audio("https://example/video", Path(directory) / "output.m4a", timeout=1)
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)

    def test_ctrl_c_stops_child_process(self):
        process = Mock()
        process.stdout = io.StringIO()
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as directory, patch("zju_cli.media.subprocess.Popen", return_value=process), \
             patch("zju_cli.media.queue.Queue") as event_queue:
            event_queue.return_value.get.side_effect = KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                extract_audio("https://example/video", Path(directory) / "output.m4a")
        process.terminate.assert_called_once()

    def test_cancel_request_stops_ffmpeg(self):
        process = Mock()
        process.stdout = io.StringIO()
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as directory, patch("zju_cli.media.subprocess.Popen", return_value=process):
            with self.assertRaises(CancelledError):
                extract_audio("https://example/video", Path(directory) / "output.m4a", should_stop=lambda: True)
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)

    @unittest.skipUnless(os.name == "nt", "Windows-specific process flag")
    def test_ffmpeg_launch_disables_console_window_and_preserves_output_capture(self):
        process = Mock()
        process.stdout = io.StringIO()
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as directory, \
             patch("zju_cli.media.subprocess.Popen", return_value=process) as spawn:
            with self.assertRaises(CancelledError):
                extract_audio("https://example/video", Path(directory) / "output.m4a", should_stop=lambda: True)
        self.assertEqual(spawn.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
        self.assertEqual(spawn.call_args.kwargs["stdout"], subprocess.PIPE)
        self.assertIsNotNone(spawn.call_args.kwargs["stderr"])
