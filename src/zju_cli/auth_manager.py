"""Serialized CAS handshakes with operation-local application cookie jars."""
from copy import copy
from dataclasses import replace
import threading
import time

from .clients.auth import CasAuth
from .clients.http import HttpSession
from .errors import AuthenticationError


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

    def _create(self, platform, timeout=None):
        if platform not in {"courses", "cmc"}:
            raise ValueError("Unknown authentication platform")
        with self.lock:
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
            except AuthenticationError:
                self.failed_until = self.clock() + 30
                raise
            finally:
                self.cookies = [copy(c) for c in session.cookies if is_cas_cookie(c)]
            return result

    def courses(self, timeout=None):
        return self._create("courses", timeout)

    def cmc(self, timeout=None):
        return self._create("cmc", timeout)
