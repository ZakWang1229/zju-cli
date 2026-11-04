from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import os
import stat
import unittest

from zju_cli.shared.runtime import runtime_dir
from zju_cli.shared.storage import default_data_dir
from zju_cli.backend.command_registry import CommandRegistry
from zju_cli.shared.config import Settings
from zju_cli.backend.modules.tasks import TasksModule


class PlatformRuntimeTests(unittest.TestCase):
    def test_absolute_xdg_paths_and_relative_fallback(self):
        with TemporaryDirectory() as root:
            state = Path(root) / "state"
            run = Path(root) / "run"
            environment = {"XDG_STATE_HOME": str(state), "XDG_RUNTIME_DIR": str(run)}
            posix = SimpleNamespace(name="posix", environ=environment)
            with patch("zju_cli.shared.storage.os", posix), patch("zju_cli.shared.runtime.os", posix):
                self.assertEqual(default_data_dir(), state / "zju-cli")
                self.assertEqual(runtime_dir(), run / "zju-cli")
                environment["XDG_RUNTIME_DIR"] = "relative"
                self.assertEqual(runtime_dir(), state / "zju-cli" / "run")
                environment["XDG_STATE_HOME"] = "relative"
                self.assertEqual(default_data_dir(), Path.home() / ".local" / "state" / "zju-cli")

    @unittest.skipUnless(os.name == "posix", "POSIX file permissions")
    def test_private_task_database_and_state_directory(self):
        with TemporaryDirectory() as root:
            daemon = CommandRegistry()
            daemon.command_registry = daemon
            daemon.state_dir = Path(root) / "state"
            module = TasksModule(daemon, Settings("u", "p"))
            try:
                self.assertEqual(stat.S_IMODE(module.root.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(module.database.stat().st_mode), 0o600)
            finally:
                module.close()
