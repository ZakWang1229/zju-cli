from http.cookiejar import Cookie
import unittest
from unittest.mock import patch

from zju_cli.auth_manager import AuthManager, is_cas_cookie
from zju_cli.clients.http import HttpSession
from zju_cli.clients.auth import COURSES, TGMEDIA
from zju_cli.config import Settings
from zju_cli.errors import AuthenticationError, RemoteError


def cookie(domain, path="/", name="CASTGC"):
    return Cookie(0, name, "secret", None, False, domain, True, domain.startswith("."),
                  path, True, True, None, True, None, None, {})


class AuthManagerTests(unittest.TestCase):
    def test_two_platforms_use_independent_jars_and_one_password_submission(self):
        sessions = []
        posts = []
        class FakeHttp(HttpSession):
            def __init__(self, timeout):
                super().__init__(timeout)
                self.landing = None
                sessions.append(self)
            def request(self, url, *, method="GET", **kwargs):
                if method == "POST":
                    posts.append(1)
                    self.cookies.set_cookie(cookie(".zju.edu.cn"))
                    return 200, b"", self.landing
                self.landing = url
                if not any(c.name == "CASTGC" for c in self.cookies):
                    return 200, b'<form><input name="execution" value="one"></form>', "https://zjuam.zju.edu.cn/cas/login"
                if url.startswith(TGMEDIA):
                    value = cookie("tgmedia.cmc.zju.edu.cn", name="JWTUser")
                    value.value = '{"sub":"test-user"}'
                    self.cookies.set_cookie(value)
                return 200, b"", url
            def json(self, url, **kwargs):
                return 200, {"modulus": "ca1", "exponent": "11"}
        manager = AuthManager(Settings("student", "password"), session_factory=FakeHttp)
        first = manager.courses()
        second, user_id = manager.cmc()
        third = manager.courses()
        self.assertEqual(len(posts), 1)
        self.assertEqual(user_id, "test-user")
        self.assertEqual(len({id(s.cookies) for s in (first, second, third)}), 3)
        self.assertEqual([c.name for c in manager.cookies], ["CASTGC"])
        self.assertFalse(any(c.name == "JWTUser" for c in third.cookies))

    def test_domain_and_path_matching(self):
        self.assertTrue(is_cas_cookie(cookie(".zju.edu.cn")))
        self.assertTrue(is_cas_cookie(cookie("zjuam.zju.edu.cn", "/cas")))
        self.assertFalse(is_cas_cookie(cookie("courses.zju.edu.cn")))
        self.assertFalse(is_cas_cookie(cookie("evilzju.edu.cn")))
        self.assertFalse(is_cas_cookie(cookie("zjuam.zju.edu.cn", "/castle")))

    def test_temporary_jars_keep_only_central_cookies(self):
        manager = AuthManager(Settings("student", "password"))
        jars = []
        class FakeAuth:
            def __init__(self, settings):
                pass
            def courses(self):
                jars.append(self._session.cookies)
                self._session.cookies.set_cookie(cookie(".zju.edu.cn"))
                self._session.cookies.set_cookie(cookie("courses.zju.edu.cn", name="site"))
                return self._session
        with patch("zju_cli.auth_manager.CasAuth", FakeAuth):
            first = manager.courses()
            second = manager.courses()
        self.assertIsNot(first.cookies, second.cookies)
        self.assertEqual([c.name for c in manager.cookies], ["CASTGC"])
        self.assertEqual(len(jars), 2)

    def test_only_authentication_failure_has_cooldown(self):
        now = [0]
        manager = AuthManager(Settings("student", "password"), clock=lambda: now[0])
        with patch("zju_cli.auth_manager.CasAuth.courses", side_effect=[RemoteError("network"),
                AuthenticationError("credentials"), HttpSession()]) as authenticate:
            with self.assertRaises(RemoteError):
                manager.courses()
            with self.assertRaises(AuthenticationError):
                manager.courses()
            with self.assertRaises(AuthenticationError):
                manager.courses()
            self.assertEqual(authenticate.call_count, 2)
            now[0] = 31
            manager.courses()
            self.assertEqual(authenticate.call_count, 3)
