"""Shared HTTP session, cookie handling, and authenticated downloads."""
from __future__ import annotations

import json
from http.client import HTTPException
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

from zju_cli.shared.errors import RemoteError, SessionExpired

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"


class HttpSession:
    def __init__(self, timeout: float = 30):
        self.timeout = timeout
        self.cookies = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies))

    def close(self):
        self.cookies.clear()

    def download(self, url, stream, *, check=None):
        request = Request(url, headers={"User-Agent": UA})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if "/cas/login" in urlsplit(response.geturl()).path:
                    raise SessionExpired("下载跳转到登录页")
                count = 0
                while True:
                    if check:
                        check()
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    stream.write(chunk)
                    count += len(chunk)
                return count
        except HTTPError as exc:
            exc.close()
            if exc.code in {401, 403}:
                raise SessionExpired("下载认证失效") from exc
            raise RemoteError(f"附件下载失败（HTTP {exc.code}）") from exc
        except (URLError, OSError, HTTPException) as exc:
            raise RemoteError("附件下载网络失败") from exc

    def request(self, url: str, *, method="GET", data=None, headers=None, allowed_statuses=()):
        request_headers = {"User-Agent": UA, "Accept": "text/html,application/json,*/*"}
        request_headers.update(headers or {})
        request = Request(url, data=data, headers=request_headers, method=method)
        parsed = urlsplit(url)
        endpoint = parsed.netloc + parsed.path
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
            raise RemoteError(f"网络请求失败或超时：{endpoint}；请检查网络后重试") from exc

    def json(self, url: str, *, method="GET", body=None, headers=None):
        request_headers = {"Accept": "application/json", **(headers or {})}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        status, raw, final_url = self.request(
            url, method=method, data=data, headers=request_headers)
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
        if "token" in message.lower() and any(
                word in message for word in ("未", "失效", "过期", "无效")):
            raise SessionExpired("接口认证 token 缺失或失效")
        if value.get("success") is False:
            raise RemoteError("远端接口报告操作失败")
        return status, value
