import io
import json
import unittest
from unittest.mock import patch

from zju_cli.cli import main, parser
from zju_cli.errors import ConfigError
from zju_cli.services.platform import ZjuService


class CliTests(unittest.TestCase):
    def test_autosign_local_status_and_stop_need_no_credentials_or_start(self):
        for command in ("status","stop","list","history"):
            with patch("zju_cli.cli.DaemonClient") as client, \
                 patch("zju_cli.cli.Settings.load") as load, \
                 patch("zju_cli.services.attendance.offline_view", return_value={"enabled":False}) as local, \
                 patch("sys.stdout",io.StringIO()):
                client.return_value.status.return_value={"running":False}
                self.assertEqual(main(["autosign",command,"--json"]),0)
                load.assert_not_called();client.return_value.call.assert_not_called()
                self.assertEqual(local.call_args.args[0],command)

    def test_autosign_answer_is_not_retried_and_invalid_date_rejected(self):
        with patch("zju_cli.cli.Settings.load",return_value=__import__('zju_cli.config',fromlist=['Settings']).Settings("user","password")), \
             patch("zju_cli.cli.DaemonClient") as client, patch("sys.stdout",io.StringIO()):
            client.return_value.call.return_value={"state":"success"}
            self.assertEqual(main(["autosign","answer","123","--code","0123","--json"]),0)
            client.return_value.call.assert_called_once()
        with patch("sys.stderr",io.StringIO()):
            self.assertEqual(main(["autosign","history","--date","bad","--json"]),2)

    def test_transcript_command_is_removed(self):
        with patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                parser().parse_args(["transcript", "--sub-id", "42", "--out", "lesson.md"])
        self.assertEqual(raised.exception.code, 2)
        self.assertFalse(hasattr(ZjuService, "transcript"))

    def test_json_at_each_level(self):
        for arguments in (["--json", "homework", "list", "--course-id", "42"],
                          ["homework", "--json", "list", "--course-id", "42"],
                          ["homework", "list", "--course-id", "42", "--json"]):
            self.assertTrue(parser().parse_args(arguments).json)

    def test_submit_requires_yes_without_terminal(self):
        with patch("sys.stdin", io.StringIO()), patch("sys.stderr", io.StringIO()), patch("zju_cli.cli.Settings.load") as load:
            self.assertEqual(main(["homework", "submit", "--activity-id", "42", "--file", "answer.pdf"]), 2)
            load.assert_not_called()

    def test_json_result_excludes_logs(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch("sys.stdout", output), patch("sys.stderr", errors), patch("zju_cli.cli.run", return_value={"count": 0, "todos": []}):
            self.assertEqual(main(["todos", "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue()), {"count": 0, "todos": []})
        self.assertEqual(errors.getvalue(), "")

    def test_json_failure_uses_stderr_and_exit_code(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch("sys.stdout", output), patch("sys.stderr", errors), patch("zju_cli.cli.run", side_effect=ConfigError("missing credentials")):
            self.assertEqual(main(["todos", "--json"]), 3)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(json.loads(errors.getvalue())["error"]["type"], "ConfigError")

    def test_refused_retraction_is_nonzero(self):
        with patch("sys.stdout", io.StringIO()), patch("zju_cli.cli.run", return_value={"ok": False}):
            self.assertEqual(main(["homework", "retract", "--activity-id", "42", "--yes"]), 4)
