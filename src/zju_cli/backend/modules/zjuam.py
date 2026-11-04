"""Serialized CAS handshakes with operation-local application cookie jars."""
from copy import copy
from dataclasses import replace
from html.parser import HTMLParser
import json
import threading
import time
from urllib.parse import unquote, urlencode, urljoin, urlsplit
import weakref

from zju_cli.shared.config import Settings
from zju_cli.shared.errors import AuthenticationError
from zju_cli.shared.http import HttpSession

COURSES = "https://courses.zju.edu.cn"
ZJUAM = "https://zjuam.zju.edu.cn"
TGMEDIA = "https://tgmedia.cmc.zju.edu.cn"


class CasFormParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms: list[dict] = []
        self.current: dict | None = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form":
            self.current = {
                "id": attributes.get("id"),
                "action": attributes.get("action"),
                "execution": None,
            }
            self.forms.append(self.current)
        elif tag == "input" and self.current is not None and attributes.get("name") == "execution":
            self.current["execution"] = attributes.get("value")

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None

    def login_form(self):
        candidates = [form for form in self.forms if form["execution"]]
        if not candidates:
            raise AuthenticationError("未找到 CAS 登录表单 execution token")
        return next((form for form in candidates if form["id"] == "fm1"), candidates[0])


def encrypt_password(password: str, modulus: str, exponent: str) -> str:
    # CAS uses raw RSA, matching security.js without PKCS#1 padding.
    message = int.from_bytes(password.encode("utf-8"), "big")
    return format(pow(message, int(exponent, 16), int(modulus, 16)), "x")


class CasAuth:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._session = None

    def login(self, landing_url: str) -> HttpSession:
        if self._session is None:
            self._session = HttpSession(self.settings.timeout_seconds)
        session = self._session
        _, raw, landing_url = session.request(landing_url)
        if urlsplit(landing_url).hostname != "zjuam.zju.edu.cn":
            # A central CAS cookie can issue a new service ticket without
            # presenting the password form again.
            return session
        parser = CasFormParser()
        parser.feed(raw.decode("utf-8", errors="replace"))
        form = parser.login_form()
        post_url = urljoin(landing_url, form["action"] or landing_url)
        parsed = urlsplit(post_url)
        if parsed.hostname != "zjuam.zju.edu.cn" or parsed.scheme != "https":
            raise AuthenticationError("CAS 表单目标地址异常")
        _, public = session.json(ZJUAM + "/cas/v2/getPubKey")
        try:
            encrypted = encrypt_password(
                self.settings.password, public["modulus"], public["exponent"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AuthenticationError("CAS 公钥格式无效") from exc
        body = urlencode({
            "username": self.settings.username,
            "password": encrypted,
            "authcode": "",
            "execution": form["execution"],
            "_eventId": "submit",
        }).encode("utf-8")
        _, _, final_url = session.request(
            post_url,
            method="POST",
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": landing_url,
                "Origin": ZJUAM,
            },
        )
        if "/cas/login" in urlsplit(final_url).path:
            raise AuthenticationError("CAS 登录失败：账号密码错误或触发验证码风控")
        return session

    def courses(self) -> HttpSession:
        return self.login(COURSES + "/login")

    def cmc(self) -> tuple[HttpSession, str]:
        session = self.login(TGMEDIA + "/")
        for cookie in session.cookies:
            if cookie.name != "JWTUser":
                continue
            try:
                info = json.loads(unquote(cookie.value))
                user_id = info.get("sub") or info.get("user_id") or info.get("id")
            except (ValueError, TypeError, AttributeError):
                continue
            if user_id:
                return session, str(user_id)
        raise AuthenticationError("登录后未取到 JWTUser/user_id")


def is_cas_cookie(cookie):
    domain = cookie.domain.lstrip(".").lower()
    host = "zjuam.zju.edu.cn"
    domain_matches = host == domain or (
        cookie.domain_specified and host.endswith("." + domain))
    path = cookie.path or "/"
    path_matches = path == "/" or "/cas" == path or "/cas/".startswith(path.rstrip("/") + "/")
    return domain_matches and path_matches


class AuthManager:
    def __init__(self, settings, *, clock=time.monotonic, session_factory=HttpSession):
        self.settings = settings
        self.clock = clock
        self.session_factory = session_factory
        self.lock = threading.Lock()
        self.cookies = []
        self.failed_until = 0
        self.last_result = None
        self.closed = False
        self.sessions = weakref.WeakSet()

    def _create(self, platform, timeout=None):
        if platform not in {"courses", "cmc"}:
            raise ValueError("Unknown authentication platform")
        with self.lock:
            if self.closed:
                raise AuthenticationError("认证模块已关闭")
            if self.clock() < self.failed_until:
                raise AuthenticationError("CAS 登录失败冷却中，请稍后重试")
            settings = replace(self.settings, timeout_seconds=(
                self.settings.timeout_seconds if timeout is None else timeout))
            session = self.session_factory(settings.timeout_seconds)
            for cookie in self.cookies:
                session.cookies.set_cookie(copy(cookie))
            auth = CasAuth(settings)
            auth._session = session
            try:
                result = getattr(auth, platform)()
                self.last_result = "success"
                self.sessions.add(session)
            except AuthenticationError:
                self.failed_until = self.clock() + 30
                self.last_result = "authentication_failed"
                raise
            finally:
                self.cookies = [copy(c) for c in session.cookies if is_cas_cookie(c)]
            return result

    def courses(self, timeout=None):
        return self._create("courses", timeout)

    def cmc(self, timeout=None):
        return self._create("cmc", timeout)

    def close(self):
        with self.lock:
            self.closed = True
            self.cookies.clear()
            self.failed_until = 0
            sessions = list(self.sessions)
            self.sessions.clear()
        for session in sessions:
            session.close()

    def status(self):
        with self.lock:
            return {"authenticated": bool(self.cookies), "last_result": self.last_result,
                    "cooldown_seconds": max(0, self.failed_until - self.clock())}


class ZjuamModule:
    def __init__(self, daemon, config, *, auth=None):
        self.daemon = daemon
        self.config = config
        self.manager = auth or AuthManager(config)
        daemon.command_registry.register("zjuam.status", self.status)

    def courses(self, timeout=None):
        return self.manager.courses(timeout)

    def cmc(self, timeout=None):
        result = self.manager.cmc(timeout)
        if isinstance(result, tuple):
            session, user_id = result
            session.user_id = user_id
            return session
        return result

    def status(self):
        return self.manager.status()

    def close(self):
        close = getattr(self.manager, "close", None)
        if close is not None:
            close()
