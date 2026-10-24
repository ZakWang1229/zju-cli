from __future__ import annotations
import json
from urllib.parse import urlencode, urlsplit
from ..errors import RemoteError
from ..validation import required_list
YJAPI = "https://yjapi.cmc.zju.edu.cn"

def playback_url(cmc, sub_id, course_id):
    session, _ = cmc
    _, data = session.json(YJAPI + "/courseapi/v2/course/catalogue?" + urlencode({"course_id": course_id}))
    result = data.get("result")
    payload = result.get("data") if isinstance(result, dict) else None
    items = payload.get("item") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise RemoteError("回放目录接口格式异常")
    item = next((row for row in items if isinstance(row, dict) and str(row.get("sub_id")) == sub_id), None)
    if item is None:
        raise RemoteError("未找到该节课的回放记录")
    content = item.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError as exc:
            raise RemoteError("回放内容格式异常") from exc
    if not isinstance(content, dict):
        raise RemoteError("该节课尚无回放内容")
    url = (content.get("save_playback") or {}).get("contents") or (content.get("playback") or {}).get("url")
    if not isinstance(url, str) or urlsplit(url).scheme not in {"http", "https"}:
        raise RemoteError("该节课尚无有效回放链接")
    return url
