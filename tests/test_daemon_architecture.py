import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from zju_cli.shared.config import Settings
from zju_cli.backend.command_registry import CommandRegistry
from zju_cli.backend.daemon import Daemon
from zju_cli.shared.errors import InputError


class CommandRegistrationTests(unittest.TestCase):
    def test_registration_metadata_and_freeze(self):
        registry = CommandRegistry()
        handler = lambda value=1: {"value": value}
        registry.register("sample.run", handler, durable=True,
                          resumable=True, serial_group="sample")
        registry.register("sample.internal", lambda: {"ok": True}, internal_only=True)
        self.assertEqual(registry.dispatch("sample.run", {"value": 2}), {"value": 2})
        self.assertNotIn("sample.internal", registry.command_list()["commands"])
        self.assertTrue(registry.command("sample.internal").internal_only)
        with self.assertRaises(InputError):
            registry.dispatch("sample.internal", {})
        self.assertEqual(registry.dispatch("sample.internal", {}, internal=True), {"ok": True})
        registry.close_registration()
        with self.assertRaises(RuntimeError):
            registry.register("late", lambda: {})

    def test_rejects_invalid_registrations(self):
        registry = CommandRegistry()
        with self.assertRaises(ValueError):
            registry.register("bad", lambda: {}, resumable=True)
        with self.assertRaises(ValueError):
            registry.register("bad-mode", lambda: {}, mode="unknown")
        registry.register("bad-result", lambda: object())
        with self.assertRaises(RuntimeError):
            registry.dispatch("bad-result", {})


class DaemonArchitectureTests(unittest.TestCase):
    def test_handle_rpc_routes_actions_and_shutdown(self):
        with tempfile.TemporaryDirectory() as temporary:
            daemon = Daemon(
                Settings("test", "secret"),
                state_dir=Path(temporary) / "state",
            )
            try:
                status, shutdown = daemon.handle_rpc({"action": "status"})
                self.assertTrue(status["running"])
                self.assertNotIn("platforms", status)
                self.assertFalse(shutdown)

                registry, shutdown = daemon.handle_rpc({"action": "command.list"})
                self.assertIn("courses.list", registry["commands"])
                self.assertFalse(shutdown)

                with patch.object(daemon, "begin_stop") as begin_stop:
                    result, shutdown = daemon.handle_rpc({"action": "stop"})
                self.assertEqual(result, {"running": False})
                self.assertTrue(shutdown)
                begin_stop.assert_called_once_with()
            finally:
                daemon.close()

    def test_command_action_uses_registry_dispatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            daemon = Daemon(
                Settings("test", "secret"),
                state_dir=Path(temporary) / "state",
            )
            try:
                with patch.object(
                        daemon.command_registry, "dispatch",
                        return_value={"running": True}) as dispatch:
                    result, shutdown = daemon.handle_rpc({
                        "action": "command",
                        "command": "daemon.status",
                        "parameters": {},
                        "username": "test",
                    })
                self.assertEqual(result, {"running": True})
                self.assertFalse(shutdown)
                dispatch.assert_called_once_with("daemon.status", {})
            finally:
                daemon.close()

    def test_command_action_allows_autosign_admin_without_config_username(self):
        with tempfile.TemporaryDirectory() as temporary:
            daemon = Daemon(
                Settings("test", "secret"),
                state_dir=Path(temporary) / "state",
            )
            try:
                with patch.object(
                        daemon.command_registry, "dispatch",
                        return_value={"enabled": False}) as dispatch:
                    result, shutdown = daemon.handle_rpc({
                        "action": "command",
                        "command": "autosign.status",
                        "parameters": {},
                        "username": None,
                    })
                self.assertEqual(result, {"enabled": False})
                self.assertFalse(shutdown)
                dispatch.assert_called_once_with("autosign.status", {})
            finally:
                daemon.close()

    def test_internal_platform_methods_use_supplied_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            auth = Mock()
            daemon = Daemon(Settings("test", "secret"), auth=auth, state_dir=Path(temporary))
            session = Mock()
            try:
                session.json.return_value = (200, {"status": "on_call"})
                self.assertEqual(daemon.courses.attendance_details("42", session=session), {"status": "on_call"})
                session.json.return_value = (200, {"result": {"data": {"item": [
                    {"sub_id": "42", "content": {"playback": {"url": "https://media.test/audio"}}}]}}})
                self.assertEqual(daemon.classroom.playback_url("42", "1", session=session), "https://media.test/audio")
                auth.courses.assert_not_called()
                auth.cmc.assert_not_called()
            finally:
                daemon.close()

    def test_partial_initialization_cleans_up_and_preserves_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            error = RuntimeError("module initialization failed")
            with patch("zju_cli.backend.daemon.Cc98Module", side_effect=error), \
                 patch("zju_cli.backend.modules.zjuam.ZjuamModule.close") as close:
                with self.assertRaises(RuntimeError) as caught:
                    Daemon(Settings("test", "secret"), state_dir=Path(temporary))
                self.assertIs(caught.exception, error)
                close.assert_called_once()
    def test_modules_register_public_names_and_share_daemon_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            daemon = Daemon(Settings("test", "secret"), state_dir=Path(temporary) / "state")
            try:
                self.assertIsInstance(daemon.command_registry, CommandRegistry)
                self.assertNotIsInstance(daemon, CommandRegistry)
                self.assertEqual(set(daemon.command_registry._commands),
                                 set(daemon.command_registry.command_list()["commands"]) | {
                    command.name for command in daemon.command_registry._commands.values()
                    if command.internal_only
                })
                self.assertIs(daemon.courses.daemon, daemon)
                self.assertIs(daemon.classroom.daemon, daemon)
                commands = daemon.command_registry.command_list()["commands"]
                self.assertIn("courses.list", commands)
                self.assertIn("classroom.schedule", commands)
                self.assertIn("autosign.status", commands)
                self.assertIn("task.submit", commands)
                self.assertIn("schedule.create", commands)
                self.assertNotIn("autosign.check", commands)
                self.assertEqual(
                    daemon.command_registry.dispatch("zjuam.status", {})["authenticated"],
                    False)
            finally:
                daemon.close()

    def test_schedule_round_is_persisted_as_a_task(self):
        import time
        called = __import__("threading").Event()
        with tempfile.TemporaryDirectory() as temporary, \
             patch("zju_cli.shared.storage.default_data_dir", return_value=Path(temporary) / "data"):
            daemon = Daemon(Settings("test", "secret"), state_dir=Path(temporary) / "state")
            daemon.courses.service.todos = lambda: (called.set() or {"count": 0})
            try:
                schedule = daemon.schedules.create("courses.todos", {}, 0.1, enabled=True)
                self.assertTrue(called.wait(3))
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    rows = daemon.tasks.list(command="courses.todos", state="succeeded")
                    if rows:
                        break
                    time.sleep(0.02)
                self.assertTrue(rows)
                self.assertEqual(rows[0]["result"], {"count": 0})
                daemon.schedules.disable(schedule)
            finally:
                daemon.close()


if __name__ == "__main__":
    unittest.main()
