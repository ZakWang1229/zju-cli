import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from zju_cli.clients.auth import CasFormParser, encrypt_password
from zju_cli.clients.http import HttpSession
from zju_cli.config import Settings
from zju_cli.errors import ConfigError, RemoteError, SessionExpired


class HttpAuthConfigTests(unittest.TestCase):
    def test_html_form_parser_selects_fm1_and_decodes_entities(self):
        form = CasFormParser()
        form.feed('<form action="/other"></form><form action="/cas/login?a=1&amp;b=2" id="fm1">'
                  '<input value="token" name="execution"></form>')
        self.assertEqual(form.login_form()["execution"], "token")
        self.assertEqual(form.login_form()["action"], "/cas/login?a=1&b=2")

    def test_rsa_known_vector(self):
        self.assertEqual(encrypt_password("A", "ca1", "11"), "ae6")

    def test_http_401_signals_expired_session(self):
        session = HttpSession()
        session.opener = Mock()
        session.opener.open.side_effect = HTTPError("https://example/api?token=secret", 401, "error", {}, io.BytesIO())
        with self.assertRaises(SessionExpired) as raised:
            session.request("https://example/api?token=secret")
        self.assertNotIn("secret", str(raised.exception))

    def test_http_timeout_hides_signed_url(self):
        session = HttpSession()
        session.opener = Mock()
        session.opener.open.side_effect = URLError("https://example/api?token=secret")
        with self.assertRaises(RemoteError) as raised:
            session.request("https://example/api?token=secret")
        self.assertNotIn("secret", str(raised.exception))

    def test_json_redirect_signals_expired_session(self):
        session = HttpSession()
        with patch.object(session, "request", return_value=(200, b"<html>", "https://zjuam.zju.edu.cn/cas/login")):
            with self.assertRaises(SessionExpired):
                session.json("https://example/api")

    def test_empty_204_is_successful_mutation(self):
        session = HttpSession()
        with patch.object(session, "request", return_value=(204, b"", "https://example/api")):
            self.assertEqual(session.json("https://example/api", method="PUT", body={}), (204, {}))

    def test_json_token_error_signals_expired_session(self):
        session = HttpSession()
        raw = json.dumps({"msg": "未传入token"}).encode()
        with patch.object(session, "request", return_value=(200, raw, "https://example/api")):
            with self.assertRaises(SessionExpired):
                session.json("https://example/api")

    def test_environment_overrides_dotenv_for_all_settings(self):
        with tempfile.TemporaryDirectory() as directory, patch("zju_cli.config.Path.home", return_value=Path(directory)):
            config = Path(directory) / "config.env"
            config.write_text("ZJU_USER=file\nZJU_PASS=private\nZJU_HTTP_TIMEOUT=15\nZJU_SESSION_TTL_MS=120000\n", encoding="utf-8")
            with patch.dict(os.environ, {"ZJU_USER": "environment"}, clear=True):
                settings = Settings.load(config)
            self.assertEqual(settings.username, "environment")
            self.assertEqual(settings.timeout_seconds, 15)
            self.assertEqual(settings.session_ttl_seconds, 120)
            self.assertNotIn("private", repr(settings))

    def test_default_credentials_are_loaded_from_zju_cli_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            credentials = home / ".secrets" / "zju-cli" / ".env"
            credentials.parent.mkdir(parents=True)
            credentials.write_text("ZJU_USER=student\nZJU_PASS=sample\n", encoding="utf-8")
            with patch("zju_cli.config.Path.home", return_value=home), \
                 patch("zju_cli.config.Path.cwd", return_value=home), patch.dict(os.environ, {}, clear=True):
                settings = Settings.load()
            self.assertEqual(settings.username, "student")
            self.assertEqual(settings.password, "sample")

    def test_nan_timeout_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch("zju_cli.config.Path.home", return_value=Path(directory)):
            config = Path(directory) / "config.env"
            config.write_text("ZJU_USER=u\nZJU_PASS=p\nZJU_HTTP_TIMEOUT=nan\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True), self.assertRaises(ConfigError):
                Settings.load(config)

    def test_worker_config_does_not_load_its_working_directory_env(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            credentials = home / ".secrets" / "zju-cli" / ".env"
            credentials.parent.mkdir(parents=True)
            credentials.write_text("ZJU_USER=student\nZJU_PASS=sample\n", encoding="utf-8")
            working = home / "worker"
            working.mkdir()
            (working / ".env").write_text("ZJU_USER=other\nZJU_PASS=other\n", encoding="utf-8")
            with patch("zju_cli.config.Path.home", return_value=home), \
                 patch("zju_cli.config.Path.cwd", return_value=working), patch.dict(os.environ, {}, clear=True):
                self.assertEqual(Settings.load().username, "other")
                self.assertEqual(Settings.load(load_cwd=False).username, "student")
