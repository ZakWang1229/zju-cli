from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock
import json
import unittest
import httpx

from zju_cli.backend.modules.cc98 import Client, download
from zju_cli.backend.command_registry import CommandRegistry
from zju_cli.shared.config import Settings
from zju_cli.shared.errors import RemoteError, LocalFileError
from zju_cli.backend.modules.webvpn import WebVPN, WebVPNModule, convert_url
from zju_cli.backend.modules.courses import MaterialsService


class ModuleServicesTests(unittest.TestCase):
    def test_webvpn_login_and_url_conversion(self):
        requests = []
        def handler(request):
            requests.append(request)
            if request.url.path == "/login":
                return httpx.Response(200, text='<input value="token" name="_csrf">')
            if request.url.path == "/do-login":
                return httpx.Response(200, json={"success": True})
            return httpx.Response(404)
        client = httpx.Client(transport=httpx.MockTransport(handler))
        vpn = WebVPN(client=client)
        try:
            vpn.login("user", "password")
            self.assertTrue(vpn.logged_in)
            self.assertIn(b"_csrf=token", requests[1].content)
            self.assertNotIn(b"password=password", requests[1].content)
            converted = convert_url("https://www.cc98.org:8443/path?x=1")
            self.assertTrue(converted.startswith("https://webvpn.zju.edu.cn/https-8443/"))
            self.assertTrue(converted.endswith("/path?x=1"))
        finally:
            vpn.close()

    def route(self, handler, mode="direct"):
        daemon = CommandRegistry()
        daemon.command_registry = daemon
        module = WebVPNModule(daemon, Settings("u", "p"))
        client = module.open_client(mode)
        client.direct.close()
        client.direct = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(module.close)
        return module, client

    def test_webvpn_auto_read_falls_back_once_and_direct_does_not(self):
        calls = []
        def unavailable(request):
            calls.append(request.url.host)
            raise httpx.ConnectError("secret query not echoed", request=request)
        module, client = self.route(unavailable, "auto")
        client.vpn.login = Mock(side_effect=lambda *args: setattr(client.vpn, "logged_in", True))
        client.vpn.client.close()
        client.vpn.client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})))
        self.assertEqual(client.request("GET", "https://api.cc98.org/me").json(), {"ok": True})
        self.assertEqual(client.route, "vpn")
        self.assertEqual(len(calls), 1)
        _, direct = self.route(unavailable)
        with self.assertRaises(httpx.ConnectError):
            direct.request("GET", "https://api.cc98.org/me")

    def test_cc98_refresh_token_before_password_and_reauthenticates_401(self):
        grants, auth_headers = [], []
        count = [0]
        def handle(request):
            if request.url.path == "/connect/token":
                body = request.content.decode()
                grants.append(body)
                return httpx.Response(200, json={"access_token": "token-" + str(len(grants)), "refresh_token": "refresh", "expires_in": 3600})
            auth_headers.append(request.headers.get("Authorization"))
            count[0] += 1
            return httpx.Response(401 if count[0] == 3 else 200, json=[])
        _, route = self.route(handle)
        client = Client(Settings("u", "p", cc98_username="forum", cc98_password="private"), route)
        self.assertEqual(client.get("/topic/new"), [])
        client.tokens["direct"]["expires"] = 0
        self.assertEqual(client.get("/topic/new"), [])
        self.assertIn("grant_type=refresh_token", grants[1])
        self.assertEqual(client.get("/topic/new"), [])
        self.assertEqual(len(grants), 3)
        client.close()
        self.assertEqual(client.tokens, {})

    def test_cc98_non_json_error_hides_response_content(self):
        def handle(request):
            if request.url.path == "/connect/token":
                return httpx.Response(200, json={"access_token": "token"})
            return httpx.Response(200, text="private-token-response")
        _, route = self.route(handle)
        client = Client(Settings("u", "p", cc98_username="forum", cc98_password="private"), route)
        with self.assertRaises(RemoteError) as caught:
            client.get("/topic/new")
        self.assertNotIn("private-token", str(caught.exception))

    def test_cc98_download_limit_and_safe_duplicate_names(self):
        _, route = self.route(lambda request: httpx.Response(200, content=b"image", headers={"Content-Type": "image/png"}))
        client = Client(Settings("u", "p"), route)
        with TemporaryDirectory() as root:
            first = download(client, "https://file.cc98.org/a.png", root, "../CON.png", image=True)
            second = download(client, "https://file.cc98.org/a.png", root, "../CON.png", image=True)
            self.assertNotEqual(first["saved"], second["saved"])
            self.assertEqual(Path(first["saved"]).parent, Path(root).resolve())
            route.direct.close()
            route.direct = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, headers={"Content-Type": "image/png", "Content-Length": str(4 * 1024 * 1024)})))
            with self.assertRaises(ValueError):
                download(client, "https://file.cc98.org/b.png", root, image=True)
            self.assertEqual(len(list(Path(root).iterdir())), 2)

    def test_materials_sync_incremental_conflict_and_account_binding(self):
        calls = []
        session = Mock()
        uploads = [{"id": 7, "name": "slides.pdf", "size": 4, "key": "v1"}]
        session.json.side_effect = lambda *a, **k: (200, {"activities": [{"id": 1, "type": "material", "uploads": uploads}], "pages": 1})
        def fetch(url, stream, check=None):
            calls.append(url)
            if check:
                check()
            stream.write(b"data")
            return 4
        session.download.side_effect = fetch
        config = Settings("account", "password")
        service = MaterialsService(config, lambda operation: operation(session))
        with TemporaryDirectory() as root:
            first = service.transfer("1", root, sync=True)
            self.assertEqual(len(first["downloaded"]), 1)
            self.assertEqual(len(service.transfer("1", root, sync=True)["skipped"]), 1)
            self.assertEqual(len(calls), 1)
            target = Path(first["downloaded"][0]["path"])
            target.write_bytes(b"my edit")
            self.assertEqual(len(service.transfer("1", root, sync=True)["conflicts"]), 1)
            self.assertEqual(target.read_bytes(), b"my edit")
            other = MaterialsService(replace(config, username="other"), lambda operation: operation(session))
            with self.assertRaises(LocalFileError):
                other.transfer("1", root, sync=True)

    def test_materials_list_deduplicates_and_single_failure_continues(self):
        session = Mock()
        session.json.return_value = (200, {"activities": [{"type": "material", "uploads": [
            {"id": 1, "name": "a.txt", "size": 1}, {"id": 1, "name": "a.txt", "size": 1},
            {"id": 2, "name": "b.txt", "size": 1}]}]})
        def fetch(url, stream, **kwargs):
            if "/1/" in url:
                raise RemoteError("missing")
            stream.write(b"b")
            return 1
        session.download.side_effect = fetch
        service = MaterialsService(Settings("account", "p"), lambda operation: operation(session))
        self.assertEqual(service.list("1")["count"], 2)
        with TemporaryDirectory() as root:
            result = service.transfer("1", root)
            self.assertFalse(result["ok"])
            self.assertEqual((len(result["failed"]), len(result["downloaded"])), (1, 1))
