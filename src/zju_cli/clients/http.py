from __future__ import annotations

import json
from http.client import HTTPException
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

from ..errors import RemoteError, SessionExpired

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"


class HttpSession:
    def __init__(self, timeout: float = 30):
        self.timeout = timeout
        self.cookies = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies))

    def request(self, url: str, *, method="GET", data=None, headers=None, allowed_statuses=()):
        request_headers = {"User-Agent": UA, "Accept": "text/html,application/json,*/*"}
        request_headers.update(headers or {})
        request = Request(url, data=data, headers=request_headers, method=method)
        endpoint = urlsplit(url).netloc + urlsplit(url).path
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.status, response.read(), response.geturl()
        except HTTPError as exc:
            code = exc.code
            if code in allowed_statuses:
                try:
                    return code, exc.read(4 * 1024 * 1024), exc.geturl()
                finally:
                    exc.close()
            exc.close()
            if code in (401, 403):
                raise SessionExpired(f"认证失效：{endpoint}（HTTP {code}）") from exc
            raise RemoteError(f"请求失败：{endpoint}（HTTP {code}）") from exc
        except (URLError, OSError, HTTPException) as exc:
            # Never echo signed URLs, response bodies, or credentials into logs.
            raise RemoteError(f"网络请求失败或超时：{endpoint}；请检查网络后重试") from exc

    def json(self, url: str, *, method="GET", body=None, headers=None):
        request_headers = {"Accept": "application/json", **(headers or {})}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        status, raw, final_url = self.request(url, method=method, data=data, headers=request_headers)
        if "/cas/login" in urlsplit(final_url).path:
            raise SessionExpired("会话失效，接口跳转到 CAS 登录页")
        if status in (204, 205) and not raw.strip():
            return status, {}
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise RemoteError("接口返回非 JSON，无法读取结果") from exc
        if not isinstance(value, dict):
            raise RemoteError("接口返回格式不正确，预期 JSON 对象")
        message = str(value.get("message") or value.get("msg") or "")
        if "token" in message.lower() and any(word in message for word in ("未", "失效", "过期", "无效")):
            raise SessionExpired("接口认证 token 缺失或失效")
        if value.get("success") is False:
            raise RemoteError("远端接口报告操作失败")
        return status, value
