import sys
import unittest
from unittest.mock import MagicMock, patch

from zju_cli.frontend.autostart import configure, main
from zju_cli.shared.config import Settings


class AutostartTests(unittest.TestCase):
    def test_enable_contains_no_credentials_and_disable_removes_value(self):
        registry = MagicMock()
        registry.QueryValueEx.return_value = ("startup-command", 1)
        settings = Settings("private-user", "private-password")
        with patch.dict(sys.modules, {"winreg": registry}), \
             patch("zju_cli.frontend.autostart.os.name", "nt"), \
             patch("zju_cli.frontend.autostart.Settings.load", return_value=settings), \
             patch("zju_cli.frontend.autostart.Path.is_file", return_value=True):
            self.assertTrue(configure("enable")["enabled"])
            command = registry.SetValueEx.call_args.args[-1]
            self.assertIn("zju_cli.frontend.autostart", command)
            self.assertNotIn(settings.username, command)
            self.assertNotIn(settings.password, command)
            registry.QueryValueEx.side_effect = FileNotFoundError
            self.assertFalse(configure("disable")["enabled"])
            registry.DeleteValue.assert_called_once()

    def test_logon_bootstrap_is_idempotent_and_uses_existing_launcher(self):
        with patch.object(sys, "argv", ["autostart"]), \
             patch("zju_cli.frontend.autostart.Settings.load", return_value=Settings("user", "password")), \
             patch("zju_cli.frontend.rpc.DaemonClient") as client:
            client.return_value.status.return_value = {"running": True}
            main()
            client.return_value._start.assert_not_called()
            client.return_value.status.return_value = {"running": False}
            main()
            client.return_value._start.assert_called_once()
