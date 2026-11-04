import importlib.util
import subprocess
import sys
import unittest


class PackageLayoutTests(unittest.TestCase):
    def test_cli_import_does_not_load_backend(self):
        program = (
            "import sys; import zju_cli.frontend.cli; "
            "assert not any(n.startswith('zju_cli.backend') for n in sys.modules)"
        )
        result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_daemon_import_does_not_load_frontend_or_legacy_executors(self):
        program = (
            "import sys; from zju_cli.backend.daemon import Daemon; "
            "assert not any(n.startswith(('zju_cli.frontend', 'zju_cli.backend.legacy')) for n in sys.modules)"
        )
        result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_business_implementations_live_in_their_modules(self):
        from zju_cli.backend.command_registry import CommandRegistry
        from zju_cli.backend.modules.autosign import AttendanceNotifications, AttendanceService
        from zju_cli.backend.modules.classroom import ClassroomService
        from zju_cli.backend.modules.courses import CoursesService, MaterialsService
        from zju_cli.backend.modules.zjuam import CasAuth
        from zju_cli.shared.http import HttpSession

        for implementation in (AttendanceNotifications, AttendanceService, CasAuth,
                               ClassroomService, CommandRegistry, CoursesService,
                               HttpSession, MaterialsService):
            self.assertIsNotNone(implementation)
        self.assertIsNone(importlib.util.find_spec("zju_cli.backend.services"))
        self.assertIsNone(importlib.util.find_spec("zju_cli.backend.legacy"))
        self.assertIsNone(importlib.util.find_spec("zju_cli.backend.cc98"))
        self.assertIsNone(importlib.util.find_spec("zju_cli.backend.clients"))
        self.assertIsNone(importlib.util.find_spec("zju_cli.backend.adapters"))
        self.assertIsNone(importlib.util.find_spec("zju_cli.backend.media"))
        self.assertIsNone(importlib.util.find_spec("zju_cli.backend.scheduling"))
        self.assertIsNotNone(importlib.util.find_spec("zju_cli.backend.command_registry"))
