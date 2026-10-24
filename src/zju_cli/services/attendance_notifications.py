"""Bounded best-effort notification delivery and repairable JSONL export."""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape


class AttendanceNotifications:
    def __init__(self, records, namespace, directory, config, *, desktop=None, ding=None):
        self.records, self.namespace = records, namespace
        self.directory, self.config = Path(directory), config
        self.desktop = desktop or self._desktop
        self.ding = ding or self._ding
        self.queue = queue.Queue(maxsize=64)
        self.pending = set()
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._worker, name="autosign-notifications", daemon=True)
        self.thread.start()

    def publish(self, notices=()):
        saved = [notice for _,notice in self.records.items(self.namespace+":notices")]
        for notice in [None, *saved, *notices]:
            key = notice["event_id"] if notice else "export"
            if notice and self.records.get(self.namespace+":notifications",key) is not None:
                continue
            with self.lock:
                if key in self.pending or self.stopping.is_set():
                    continue
                self.pending.add(key)
                try:
                    self.queue.put_nowait((key, notice))
                except queue.Full:
                    self.pending.discard(key)

    def export(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        known = {}
        for event in self.records.events(self.namespace, pending=True):
            path = self.directory / ("autosign-" + event["confirmed_at"][:10] + ".jsonl")
            if path not in known:
                known[path] = set()
                if path.exists():
                    with path.open(encoding="utf-8") as stream:
                        for line in stream:
                            try:
                                known[path].add(json.loads(line)["event_id"])
                            except (ValueError, KeyError):
                                pass
            if event["event_id"] not in known[path]:
                needs_separator = False
                if path.exists() and path.stat().st_size:
                    with path.open("rb") as stream:
                        stream.seek(-1,os.SEEK_END)
                        needs_separator = stream.read(1) != b"\n"
                with path.open("a", encoding="utf-8") as stream:
                    if needs_separator:
                        stream.write("\n")
                    stream.write(json.dumps(event, ensure_ascii=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                known[path].add(event["event_id"])
            self.records.exported(self.namespace, event["event_id"])

    def _worker(self):
        while not self.stopping.is_set():
            try:
                key, notice = self.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if notice is None:
                    self.export()
                    self.records.put(self.namespace+":delivery","error",{"error": None})
                else:
                    marker = self.namespace + ":notifications"
                    if self.records.get(marker, key) is None:
                        errors = []
                        config = self.config()
                        for active, deliver in ((config["desktop"] and os.name == "nt", self.desktop),
                                                (bool(config["dingtalk"]), self.ding)):
                            if active:
                                try:
                                    deliver(notice["message"], config)
                                except Exception as exc:
                                    errors.append(type(exc).__name__)
                        self.records.put(marker, key, {"attempted": True, "errors": errors})
            except Exception as exc:
                # Do not persist the exception text (may contain a webhook token).
                try:
                    self.records.put(self.namespace + ":delivery", "error", {"error": type(exc).__name__})
                except Exception:
                    pass
            finally:
                with self.lock:
                    self.pending.discard(key)
                self.queue.task_done()

    @staticmethod
    def _desktop(message, config):
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                r"Software\Classes\AppUserModelId\zju-cli", 0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key,"DisplayName",0,winreg.REG_SZ,"zju-cli")
        xml = '<toast><visual><binding template="ToastGeneric"><text>zju autosign</text><text>' + escape(message) + '</text></binding></visual></toast>'
        encoded = base64.b64encode(xml.encode()).decode()
        script = ("$ErrorActionPreference='Stop'; "
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null; "
            "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] > $null; "
            "$xml=New-Object Windows.Data.Xml.Dom.XmlDocument; "
            f"$xml.LoadXml([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'))); "
            "$toast=[Windows.UI.Notifications.ToastNotification]::new($xml); "
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('zju-cli').Show($toast)")
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            timeout=config["notification_timeout"], check=True, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)

    @staticmethod
    def _ding(message, config):
        ding = config["dingtalk"]
        url = ding["webhook"]
        if ding.get("secret"):
            stamp = str(int(time.time()*1000))
            signature = base64.b64encode(hmac.new(ding["secret"].encode(),
                (stamp + "\n" + ding["secret"]).encode(), hashlib.sha256).digest()).decode()
            url += ("&" if "?" in url else "?") + urlencode({"timestamp": stamp, "sign": signature})
        request = Request(url, data=json.dumps({"msgtype": "text", "text": {"content": message}}).encode(),
            headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=config["notification_timeout"]) as response:
            value = json.loads(response.read(1024*1024))
            if value.get("errcode") != 0:
                raise RuntimeError("DingTalk rejected notification")

    def close(self):
        self.stopping.set()
        self.thread.join(timeout=31)
