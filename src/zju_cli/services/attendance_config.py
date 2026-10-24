"""Attendance policy and bounded radar geometry, independent of platform APIs."""
import copy
import math
from urllib.parse import urlsplit

from ..errors import ConfigError

POINTS = [[120.089136,30.302331], [120.085042,30.30173], [120.077135,30.305142],
    [120.122176,30.261555], [120.123853,30.262544], [120.120344,30.263907],
    [120.126008,30.192908], [120.195939,30.272068], [120.198193,30.270419],
    [120.124267,30.19139], [120.124001,30.265735], [120.073427,30.299757]]
DEFAULT = {"interval": 4, "desktop": True, "notification_timeout": 5,
    "radar": {"location": POINTS[0], "candidates": POINTS, "estimate": True, "max_attempts": 14},
    "dingtalk": None}


def validate_config(value=None):
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - set(DEFAULT):
        raise ConfigError("autosign 配置字段无效")
    config = copy.deepcopy(DEFAULT)
    config.update(value)
    if not isinstance(config["radar"], dict) or set(config["radar"]) - set(DEFAULT["radar"]):
        raise ConfigError("雷达配置字段无效")
    config["radar"] = {**copy.deepcopy(DEFAULT["radar"]), **config["radar"]}
    for key in ("interval", "notification_timeout"):
        number = config[key]
        if isinstance(number, bool) or not isinstance(number, (int,float)) or not math.isfinite(number) or number <= 0 or number > 86400:
            raise ConfigError("轮询间隔和通知超时必须为有限正数")
    if config["notification_timeout"] > 30 or not isinstance(config["desktop"], bool):
        raise ConfigError("桌面通知配置无效，超时最多 30 秒")
    radar = config["radar"]
    if not isinstance(radar["estimate"], bool) or type(radar["max_attempts"]) is not int or not 1 <= radar["max_attempts"] <= 32:
        raise ConfigError("雷达尝试次数必须为 1–32")
    if not isinstance(radar["candidates"], list) or len(radar["candidates"]) > 30:
        raise ConfigError("候选地点最多 30 个")
    for point in ([radar["location"]] if radar["location"] is not None else []) + radar["candidates"]:
        if (not isinstance(point, (list,tuple)) or len(point) != 2 or
                any(type(n) not in (float,int) or not math.isfinite(n) for n in point) or
                not -180 <= point[0] <= 180 or not -90 <= point[1] <= 90):
            raise ConfigError("雷达地点必须为有效经纬度数组")
    ding = config["dingtalk"]
    if ding is not None:
        if not isinstance(ding, dict) or set(ding) - {"webhook", "secret"}:
            raise ConfigError("钉钉配置无效")
        if not isinstance(ding.get("webhook"),str) or not isinstance(ding.get("secret", ""),str):
            raise ConfigError("钉钉 webhook 和 secret 必须为字符串")
        url = urlsplit(ding.get("webhook", ""))
        if url.scheme != "https" or url.hostname != "oapi.dingtalk.com" or url.path != "/robot/send" or url.username or url.password:
            raise ConfigError("钉钉 webhook 地址无效")
    return config


def distance(point, other):
    lon, lat, x, y = map(math.radians, (*point, *other))
    h = math.sin((lat-y)/2)**2 + math.cos(lat)*math.cos(y)*math.sin((lon-x)/2)**2
    return 6372999.26 * 2 * math.asin(math.sqrt(min(1, max(0,h))))


def estimate_position(samples):
    samples = [(tuple(p),float(d)) for p,d in samples if type(d) in (int,float) and math.isfinite(d) and d > 0]
    if len({p for p,d in samples}) < 3:
        return None
    point = [sum(p[i] for p,d in samples)/len(samples) for i in (0,1)]
    for _ in range(30):
        aa=ab=bb=ar=br=0.0
        for p, observed in samples:
            base = distance(point,p)
            a = (distance([point[0]+1e-6,point[1]],p)-base)/1e-6
            b = (distance([point[0],point[1]+1e-6],p)-base)/1e-6
            residual = observed-base
            aa+=a*a; ab+=a*b; bb+=b*b; ar+=a*residual; br+=b*residual
        det=aa*bb-ab*ab
        if det <= max(aa*bb,1)*1e-12:
            return None
        dx=(bb*ar-ab*br)/det; dy=(aa*br-ab*ar)/det
        if not all(math.isfinite(n) and abs(n) < 5 for n in (dx,dy)):
            return None
        point[0]+=dx; point[1]+=dy
        if abs(dx)+abs(dy)<1e-10:
            break
    rms=math.sqrt(sum((distance(point,p)-d)**2 for p,d in samples)/len(samples))
    return point if rms <= 100 and -180 <= point[0] <= 180 and -90 <= point[1] <= 90 else None
