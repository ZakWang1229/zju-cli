"""Independent route clients; all authentication stays in the daemon."""
import threading
from html.parser import HTMLParser
from urllib.parse import urlsplit
import httpx

from zju_cli.shared.errors import AuthenticationError, InputError

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
try:
    from cryptography.hazmat.decrepit.ciphers.modes import CFB
except ImportError:
    from cryptography.hazmat.primitives.ciphers.modes import CFB

BASE = "https://webvpn.zju.edu.cn"
URL_KEY = "wrdvpnisthebest!"
PWD_KEY = "wrdvpnisawesome!"


def encrypt(prefix, plaintext):
    raw = plaintext.encode("utf-8")
    padded = raw + b"\0" * (-len(raw) % 16)
    cipher = Cipher(algorithms.AES(prefix.encode()), CFB(prefix.encode())).encryptor()
    # JavaScript's string length is measured in UTF-16 code units.
    length = len(plaintext.encode("utf-16-le")) // 2
    return prefix.encode("ascii").hex() + (cipher.update(padded) + cipher.finalize()).hex()[:2 * length]


def convert_url(url):
    u = urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
        raise ValueError("仅支持不含用户信息的 http/https 地址")
    if u.hostname == "webvpn.zju.edu.cn" and u.scheme == "https" and u.port in (None, 443):
        return url
    port = u.port
    prop = u.scheme + (f"-{port}" if port and port != (443 if u.scheme == "https" else 80) else "")
    return f"{BASE}/{prop}/{encrypt(URL_KEY, u.hostname)}{u.path or '/'}" + (f"?{u.query}" if u.query else "")


class WebVPN:
    def __init__(self, client=None, timeout=30):
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=True, trust_env=False)
        self.enabled = False
        self.logged_in = False

    def login(self, username, password):
        page = self.client.get(BASE + "/login")
        page.raise_for_status()
        # Parse input attributes independently of their order.
        class Inputs(HTMLParser):
            def __init__(self):
                super().__init__(); self.values = {}
            def handle_starttag(self, tag, attrs):
                a = dict(attrs)
                if tag.lower() == "input" and a.get("name"):
                    self.values[a["name"]] = a.get("value", "")
        parser = Inputs(); parser.feed(page.text)
        csrf = parser.values.get("_csrf")
        if not csrf:
            raise RuntimeError("WebVPN 登录失败：无法获取 CSRF token")
        response = self.client.post(BASE + "/do-login", data={"_csrf": csrf, "auth_type": "local", "sms_code": "", "captcha": "", "needCaptcha": "false", "captcha_id": parser.values.get("captcha_id", ""), "username": username, "password": encrypt(PWD_KEY, password)})
        response.raise_for_status(); result = response.json()
        if result.get("error") == "NEED_CONFIRM":
            response = self.client.post(BASE + "/do-confirm-login", content=b"")
            response.raise_for_status(); result = response.json()
        if not result.get("success"):
            if result.get("error") == "CAPTCHA_FAILED":
                raise RuntimeError("WebVPN 登录需要验证码，请在浏览器登录 WebVPN 或使用其他网络后重试")
            raise RuntimeError("WebVPN 登录失败，请检查账号或网络")
        self.logged_in = True

    def close(self):
        self.client.close()


class WebVPNClient:
    def __init__(self, config, mode, timeout):
        self.config, self.mode = config, mode
        self.direct = httpx.Client(timeout=timeout, follow_redirects=True, trust_env=False)
        self.vpn = WebVPN(timeout=timeout)
        self.route = "vpn" if mode == "vpn" else "direct"
        self.closed = False
        self.lock = threading.RLock()

    def use_vpn(self):
        if not self.vpn.logged_in:
            try:
                self.vpn.login(self.config.username, self.config.password)
            except RuntimeError as exc:
                raise AuthenticationError(str(exc)) from exc
        self.vpn.enabled = True
        self.route = "vpn"

    def request(self, method, url, **kwargs):
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise InputError("仅支持不含用户信息的 http/https 地址")
        with self.lock:
            if self.closed:
                raise InputError("访问客户端已关闭")
            if self.route == "vpn":
                self.use_vpn()
                return self.vpn.client.request(method, convert_url(url), **kwargs)
            try:
                response = self.direct.request(method, url, **kwargs)
                if response.status_code >= 500 and self.mode == "auto" and method.upper() in {"GET", "HEAD"}:
                    response.close()
                    self.use_vpn()
                    return self.vpn.client.request(method, convert_url(url), **kwargs)
                return response
            except httpx.RequestError:
                if self.mode != "auto" or method.upper() not in {"GET", "HEAD"}:
                    raise
                self.use_vpn()
                return self.vpn.client.request(method, convert_url(url), **kwargs)

    def close(self):
        with self.lock:
            if not self.closed:
                self.closed = True
                self.direct.close()
                self.vpn.close()


class WebVPNModule:
    def __init__(self, daemon, config):
        self.daemon, self.config = daemon, config
        self.clients = []
        self.lock = threading.Lock()
        self.closed = False
        daemon.command_registry.register("webvpn.status", self.status)

    def open_client(self, mode="auto", timeout=30):
        if mode not in {"auto", "direct", "vpn"}:
            raise InputError("无效 WebVPN 模式")
        with self.lock:
            if self.closed:
                raise InputError("WebVPN 模块已关闭")
            client = WebVPNClient(self.config, mode, timeout)
            self.clients.append(client)
            return client

    def status(self):
        with self.lock:
            return {"active_clients": sum(not c.closed for c in self.clients),
                    "logged_in": any(c.vpn.logged_in and not c.closed for c in self.clients),
                    "routes": [c.route for c in self.clients if not c.closed]}

    def close(self):
        with self.lock:
            self.closed = True
            clients, self.clients = self.clients, []
        for client in clients:
            client.close()
