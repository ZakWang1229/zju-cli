from types import SimpleNamespace
from pathlib import Path
import math
import mimetypes
import re
import threading
import time
import httpx
from urllib.parse import quote, unquote, urlsplit

from zju_cli.backend.modules.webvpn import convert_url
from zju_cli.shared.errors import (
    AuthenticationError,
    ConfigError,
    InputError,
    LocalFileError,
    RemoteError,
)

API = "https://api.cc98.org"
TOKEN = "https://openid.cc98.org/connect/token"
CLIENT_ID = "9a1fd200-8687-44b1-4c20-08d50a96e5cd"
CLIENT_SECRET = "8b53f727-08e2-4509-8857-e34bf92b27f2"


def network_error(exc):
    return type(exc).__name__


def ubb_to_text(content):
    text = str(content or "")
    for tag, label in (("img", "图片"), ("video", "视频"), ("audio", "音频"), ("upload", "文件")):
        text = re.sub(rf"\[{tag}(?:=[^\]]*)?\]([\s\S]*?)\[/{tag}\]",
                      lambda match: f"[{label}: {match[1]}]", text, flags=re.I)
    text = re.sub(r"\[upload(?:=[^\]]*)?\](https?://[^\s\[]+)", r"[文件: \1]", text, flags=re.I)
    text = re.sub(r"\[url=([^\]]*)\]([\s\S]*?)\[/url\]", r"\2 (\1)", text, flags=re.I)
    text = re.sub(r"\[url\]([\s\S]*?)\[/url\]", r"\1", text, flags=re.I)
    text = re.sub(r"\[quote\]", "\n> [引用] ", text, flags=re.I)
    text = re.sub(r"\[/quote\]", "\n", text, flags=re.I)
    text = re.sub(
        r"\[/?(?:b|i|u|del|center|left|right|line|noubb|color|size|font|align|bg|sound|upload)(?:=[^\]]*)?\]",
        "", text, flags=re.I)
    text = re.sub(r"\[(?:ac|em|cc98|tb|ms)\d+\]", "[表情]", text, flags=re.I)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def safe_name(name):
    name = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', "_", name).strip(" .")[:200].rstrip(" .")
    if not name or name in (".", ".."):
        name = "cc98-file"
    if re.match(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", name, re.I):
        name = "_" + name
    return name


def topic(value):
    return {
        "id": value.get("id"),
        "title": value.get("title"),
        "boardId": value.get("boardId"),
        "boardName": value.get("boardName"),
        "author": "(匿名)" if value.get("isAnonymous") else value.get("userName"),
        "replyCount": value.get("replyCount"),
        "hitCount": value.get("hitCount"),
        "likeCount": value.get("likeCount"),
        "createTime": value.get("time"),
        "lastPostTime": value.get("lastPostTime"),
    }


def post(value):
    result = {
        "floor": value.get("floor"),
        "author": "(匿名" + (value.get("userName") or "") + ")" if value.get("isAnonymous") else value.get("userName"),
        "time": value.get("time"),
        "content": "(已删除)" if value.get("isDeleted") else ubb_to_text(value.get("content")),
        "likeCount": value.get("likeCount"),
        "dislikeCount": value.get("dislikeCount"),
    }
    if value.get("awards"):
        result["awards"] = [
            f"{award.get('content', '')}({award.get('reason') or ''})"
            for award in value["awards"][:5]
        ]
    return result


class Client:
    def __init__(self, config, route):
        self.config, self.route_client = config, route
        self.tokens = {}

    @property
    def vpn(self):
        return self.route_client.vpn

    @property
    def transport(self):
        return self.vpn.client if self.route_client.route == "vpn" else self.route_client.direct

    def initialize(self):
        if self.route_client.mode == "vpn":
            self.route_client.use_vpn()

    def target(self, url):
        return convert_url(url) if self.route_client.route == "vpn" else url

    def fetch_token(self, grant, **fields):
        response = self.route_client.request(
            "POST",
            TOKEN,
            data={
                "grant_type": grant,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "scope": "cc98-api openid offline_access",
                **fields,
            },
        )
        if response.status_code >= 500:
            response.raise_for_status()
        try:
            result = response.json()
        except ValueError:
            result = {}
        if (not isinstance(result, dict) or not response.is_success
                or not isinstance(result.get("access_token"), str)):
            if grant == "refresh_token":
                return None
            raise AuthenticationError(f"CC98 登录失败（HTTP {response.status_code}）")
        try:
            expires = float(result.get("expires_in", 3600))
            if not math.isfinite(expires) or expires <= 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise RemoteError("CC98 Token 有效期格式无效") from exc
        return {
            "access": result["access_token"],
            "refresh": result.get("refresh_token"),
            "expires": time.monotonic() + expires - min(60, expires / 2),
        }

    def ensure_token(self):
        route = self.route_client.route
        token = self.tokens.get(route)
        if token and token["expires"] > time.monotonic():
            return token["access"]
        refreshed = (
            self.fetch_token("refresh_token", refresh_token=token["refresh"])
            if token and token.get("refresh") else None
        )
        if refreshed is None:
            refreshed = self.fetch_token(
                "password",
                username=self.config.cc98_username,
                password=self.config.cc98_password,
            )
        self.tokens[route] = refreshed
        return refreshed["access"]

    def get(self, path, params=None, search=False):
        self.initialize()
        try:
            return self._get(path, params, search)
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            if (self.route_client.mode != "auto" or self.route_client.route == "vpn"
                    or isinstance(exc, httpx.HTTPStatusError)
                    and exc.response.status_code < 500):
                raise RemoteError("CC98 网络请求失败：" + network_error(exc)) from exc
            self.route_client.use_vpn()
            try:
                return self._get(path, params, search)
            except httpx.HTTPError as fallback:
                raise RemoteError("CC98 WebVPN 回退失败：" + network_error(fallback)) from fallback

    def _get(self, path, params, search):
        response = None
        for attempt in range(3):
            route = self.route_client.route
            token = self.ensure_token()
            response = self.route_client.request(
                "GET",
                API + path,
                params=params,
                headers={"Authorization": "Bearer " + token},
            )
            if self.route_client.route != route:
                continue
            if response.status_code == 401 and attempt < 2:
                self.tokens.pop(route, None)
                continue
            if response.status_code == 429 and search and attempt == 0:
                try:
                    delay = min(60, max(1, float(response.headers.get("retry-after", "5"))))
                    if not math.isfinite(delay):
                        delay = 5
                except ValueError:
                    delay = 5
                time.sleep(delay)
                continue
            break
        response.raise_for_status()
        try:
            return response.json()
        except ValueError as exc:
            raise RemoteError("CC98 返回非 JSON 内容") from exc

    def close(self):
        self.tokens.clear()
        self.route_client.close()


def _download_once(client, url, directory, filename, image, check):
    parsed = urlsplit(url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password):
        raise ValueError("仅支持不含用户信息的 http/https 地址")
    client.initialize()
    limit = int(3.75 * 1024 * 1024) if image else 100 * 1024 * 1024
    with client.transport.stream(
            "GET", client.target(url), headers={"Referer": "https://www.cc98.org/"}) as response:
        response.raise_for_status()
        mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if image and not mime.startswith("image/"):
            raise ValueError(f"该地址不是图片（content-type: {mime or '未知'}）")
        try:
            length = int(response.headers.get("content-length", "0"))
        except ValueError:
            length = 0
        if length > limit:
            raise ValueError("文件超过大小限制")
        directory = Path(directory or "~/Downloads/cc98").expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        name = safe_name(filename if filename is not None else unquote(parsed.path.rsplit("/", 1)[-1]))
        if not Path(name).suffix:
            name += {
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/gif": ".gif",
                "image/webp": ".webp",
            }.get(mime, mimetypes.guess_extension(mime) or "")
        path = directory / name
        index = 0
        while True:
            try:
                stream = path.open("xb")
                break
            except FileExistsError:
                index += 1
                path = directory / f"{Path(name).stem} ({index}){Path(name).suffix}"
        try:
            count = 0
            with stream:
                for chunk in response.iter_bytes():
                    if check:
                        check()
                    count += len(chunk)
                    if count > limit:
                        raise ValueError("文件超过大小限制")
                    stream.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    return {"saved": str(path), "bytes": count, "mimeType": mime}


def download(client, url, directory=None, filename=None, image=False, check=None):
    try:
        return _download_once(client, url, directory, filename, image, check)
    except (httpx.RequestError, httpx.HTTPStatusError) as exc:
        route = client.route_client
        if (route.mode != "auto" or route.route == "vpn"
                or isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500):
            raise
        route.use_vpn()
        return _download_once(client, url, directory, filename, image, check)


class Forum:
    def __init__(self, client):
        self.client = client

    def run(self, args):
        get = self.client.get
        command = args.command
        start = getattr(args, "offset", 0)
        size = getattr(args, "size", 20)
        page = {"from": start, "size": size}
        if command == "status":
            current = get("/me")
            return {
                "loggedInAs": current.get("name"),
                "userId": current.get("id"),
                "network": {
                    "webVpnEnabled": self.client.vpn.enabled,
                    "webVpnLoggedIn": self.client.vpn.logged_in,
                },
            }
        if command == "posts":
            thread = get(f"/topic/{args.id}")
            posts = get(f"/Topic/{args.id}/post", params=page)
            return {
                "topic": topic(thread),
                "totalFloors": (thread.get("replyCount") or 0) + 1,
                "nextFrom": start + len(posts),
                "posts": [post(value) for value in posts],
            }
        if command == "boards":
            return [
                {
                    "area": area.get("name"),
                    "boards": [
                        {
                            "id": board.get("id"),
                            "name": board.get("name"),
                            "description": (board.get("description") or "")[:50],
                            "todayCount": board.get("todayCount"),
                        }
                        for board in area.get("boards", [])
                    ],
                }
                for area in get("/Board/all")
            ]
        if command == "user":
            user = get(
                f"/user/{args.id}" if args.id is not None else "/user/name/" + quote(args.name, safe=""))
            try:
                recent = get(
                    f"/user/{user['id']}/recent-topic",
                    params={"userid": user["id"], **page},
                )
            except Exception:
                return {
                    "user": {
                        key: user.get(key)
                        for key in ("id", "name", "introduction", "postCount", "fanCount",
                                    "registerTime", "lastLogOnTime")
                    },
                    "warnings": ["查询用户最近主题失败，请稍后重试"],
                }
            return {
                "user": {
                    key: user.get(key)
                    for key in ("id", "name", "introduction", "postCount", "fanCount",
                                "registerTime", "lastLogOnTime")
                },
                "nextFrom": start + len(recent),
                "recentTopics": [topic(value) for value in recent],
            }
        if command == "hot":
            data = (
                get("/config/index").get("hotTopic", [])
                if args.range == "today" else get("/topic/hot-" + args.range)
            )
            items = data[start:start + size]
            return {"hotTopics": [topic(value) for value in items], "nextFrom": start + len(items)}
        if command == "search":
            items = get("/topic/search", params={"keyword": args.keyword, **page}, search=True)
        elif command == "new":
            items = get("/topic/new", params=page)
        else:
            path = f"/topic/best/board/{args.id}" if args.best else f"/board/{args.id}/topic"
            items = get(path, params=page)
        result = {"nextFrom": start + len(items), "topics": [topic(value) for value in items]}
        if command == "search":
            result["count"] = len(items)
        return result


class Cc98Module:
    def __init__(self, daemon, config):
        self.daemon, self.config = daemon, config
        self.clients = {}
        self.lock = threading.RLock()
        self.last_search = 0.0
        for name in ("search", "posts", "hot", "new", "boards", "board", "user", "image", "download", "status"):
            daemon.command_registry.register("cc98." + name, getattr(self, name),
                mode="job" if name in {"image", "download"} else "request",
                durable=name in {"image", "download"}, serial_group="cc98")

    def _client(self, mode):
        mode = mode or self.config.cc98_webvpn_mode
        if mode not in {"auto", "direct", "vpn"}:
            raise InputError("无效网络模式")
        if mode not in self.clients:
            self.clients[mode] = Client(self.config, self.daemon.webvpn.open_client(mode, self.config.timeout_seconds))
        return self.clients[mode]

    def _call(self, operation, webvpn_mode=None, **values):
        if not self.config.cc98_username or not self.config.cc98_password:
            raise ConfigError("请配置 CC98_USERNAME / CC98_PASSWORD")
        with self.lock:
            client = self._client(webvpn_mode)
            timeout = self.daemon.task.http_timeout
            client.route_client.direct.timeout = httpx.Timeout(timeout)
            client.vpn.client.timeout = httpx.Timeout(timeout)
            if operation == "search":
                wait = max(0, 1 - (time.monotonic() - self.last_search))
                if wait:
                    time.sleep(wait)
            try:
                self.daemon.task.check_cancelled()
                if operation in {"image", "download"}:
                    return download(client, values["url"], values.get("directory"), values.get("filename"), operation == "image", check=self.daemon.task.check_cancelled)
                result = Forum(client).run(SimpleNamespace(command=operation, **values))
                return {"areas": result} if isinstance(result, list) else result
            except httpx.HTTPError as exc:
                raise RemoteError("CC98 网络请求失败：" + type(exc).__name__) from exc
            except OSError as exc:
                raise LocalFileError("无法保存 CC98 附件") from exc
            except (ValueError, KeyError, TypeError) as exc:
                raise RemoteError("CC98 响应格式无效") from exc
            finally:
                if operation == "search":
                    self.last_search = time.monotonic()

    @staticmethod
    def _page(offset, size):
        if type(offset) is not int or offset < 0 or type(size) is not int or not 1 <= size <= 20:
            raise InputError("offset 必须非负，size 必须在 1–20 之间")

    def search(self, keyword, offset=0, size=20, webvpn_mode=None):
        self._page(offset, size)
        if not isinstance(keyword, str) or not keyword.strip():
            raise InputError("搜索词不能为空")
        return self._call("search", webvpn_mode, keyword=keyword, offset=offset, size=size)

    def posts(self, id, offset=0, size=10, webvpn_mode=None):
        self._page(offset, size)
        return self._call("posts", webvpn_mode, id=self._id(id), offset=offset, size=size)

    def hot(self, range="today", offset=0, size=20, webvpn_mode=None):
        self._page(offset, size)
        if range not in {"today", "weekly", "monthly"}:
            raise InputError("无效热榜范围")
        return self._call("hot", webvpn_mode, range=range, offset=offset, size=size)

    def new(self, offset=0, size=20, webvpn_mode=None):
        self._page(offset, size)
        return self._call("new", webvpn_mode, offset=offset, size=size)

    def boards(self, webvpn_mode=None):
        return self._call("boards", webvpn_mode)

    def board(self, id, best=False, offset=0, size=20, webvpn_mode=None):
        self._page(offset, size)
        return self._call("board", webvpn_mode, id=self._id(id), best=best, offset=offset, size=size)

    def user(self, id=None, name=None, offset=0, size=20, webvpn_mode=None):
        self._page(offset, size)
        if (id is None) == (name is None):
            raise InputError("需且只能提供用户 id 或 name")
        return self._call("user", webvpn_mode, id=self._id(id) if id is not None else None, name=name, offset=offset, size=size)

    def image(self, url, directory=None, filename=None, webvpn_mode=None):
        return self._call("image", webvpn_mode, url=url, directory=directory, filename=filename)

    def download(self, url, directory=None, filename=None, webvpn_mode=None):
        return self._call("download", webvpn_mode, url=url, directory=directory, filename=filename)

    def status(self, webvpn_mode=None):
        with self.lock:
            client = self.clients.get(webvpn_mode or self.config.cc98_webvpn_mode)
            return {"configured": bool(self.config.cc98_username and self.config.cc98_password),
                    "authenticated": bool(client and client.tokens),
                    "route": client.route_client.route if client else None}

    @staticmethod
    def _id(value):
        from zju_cli.shared.validation import identifier
        return identifier(value)

    def close(self):
        with self.lock:
            for client in self.clients.values():
                client.close()
            self.clients.clear()
