from __future__ import annotations

import json
from html.parser import HTMLParser
from urllib.parse import unquote, urlencode, urljoin, urlsplit

from ..config import Settings
from ..errors import AuthenticationError
from .http import HttpSession

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
            self.current = {"id": attributes.get("id"), "action": attributes.get("action"), "execution": None}
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
    # CAS uses raw RSA, matching security.js (without PKCS#1 padding).
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
            # An existing CAS SSO cookie can issue a new service ticket and
            # return directly to the application without a password form.
            return session
        parser = CasFormParser()
        parser.feed(raw.decode("utf-8", errors="replace"))
        form = parser.login_form()
        post_url = urljoin(landing_url, form["action"] or landing_url)
        if urlsplit(post_url).hostname != "zjuam.zju.edu.cn" or urlsplit(post_url).scheme != "https":
            raise AuthenticationError("CAS 表单目标地址异常")
        _, public = session.json(ZJUAM + "/cas/v2/getPubKey")
        try:
            encrypted = encrypt_password(self.settings.password, public["modulus"], public["exponent"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AuthenticationError("CAS 公钥格式无效") from exc
        body = urlencode({
            "username": self.settings.username, "password": encrypted,
            "authcode": "", "execution": form["execution"], "_eventId": "submit",
        }).encode("utf-8")
        _, _, final_url = session.request(post_url, method="POST", data=body, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": landing_url, "Origin": ZJUAM,
        })
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
