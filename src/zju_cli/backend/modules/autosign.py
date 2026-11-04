"""Attendance policy uses courses; its rounds run through schedules and tasks."""
from dataclasses import dataclass
import time

from zju_cli.shared.errors import InputError
from zju_cli.shared.records import RecordStore


@dataclass(frozen=True)
class NextRun:
    delay: float


class AutosignModule:
    def __init__(self, daemon, config):
        self.daemon, self.config = daemon, config
        self.service = AttendanceService(
            daemon, config.username, daemon.courses.attendance_adapter)
        self.notifications = AttendanceNotifications(self.service.records, self.service.namespace,
            daemon.state_dir, lambda: self.service.config)
        self.service.notifications = self.notifications
        for name, handler, options in (
            ("start", self.start, {"durable": True}), ("stop", self.stop, {}),
            ("status", self.status, {}), ("list", self.list, {}),
            ("discover", self.discover, {}),
            ("answer", self.answer, {"durable": True, "transient_fields": ("code",), "serial_group": "autosign"}),
            ("history", self.history, {}),
            ("check", self.check, {"mode": "job", "durable": True, "resumable": True,
                                    "internal_only": True, "serial_group": "autosign"}),
        ):
            daemon.command_registry.register("autosign." + name, handler, **options)

    def restore(self):
        self.service.restore()
        self.notifications.publish()

    def start(self, settings=None, interval=None, config=None):
        values = settings if settings is not None else config
        if interval is not None:
            values = {**(values or {}), "interval": interval}
        return self.service.start(values)

    def stop(self):
        result = self.service.stop()
        row = self.daemon.schedules.get("autosign")
        if row["current_task_id"]:
            self.daemon.tasks.cancel(row["current_task_id"])
        return result

    def status(self):
        return self.service.status()

    def list(self):
        return self.service.activities()

    def discover(self):
        self.service.check(read_only=True)
        status = self.service.status()
        return {**self.service.activities(), **status, "ok": status["error"] is None}

    def answer(self, activity_id, code):
        return self.service.answer(activity_id, code)

    def history(self, date=None):
        return self.service.history(date)

    def check(self):
        delay = self.service.check().delay
        self.daemon.schedules.set_next_delay("autosign", delay)
        return self.service.status()

    def close(self):
        self.service.shutdown()
        self.notifications.close()


"""One serialized attendance round. All writes are preceded by durable intent."""
from datetime import datetime, timedelta, timezone
import re
import threading
import uuid

from zju_cli.backend.modules.courses import completed
from zju_cli.shared.errors import AuthenticationError, ConfigError, InputError, SessionExpired
from zju_cli.shared.attendance_config import validate_config, estimate_position

SHANGHAI = timezone(timedelta(hours=8))


def now_text():
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


class AttendanceService:
    TASK_ID = "autosign"

    def __init__(self, daemon, username, adapter_factory, *, notifications=None, clock=now_text):
        self.daemon, self.username = daemon, username
        self.records = RecordStore(daemon.state_dir / "scheduler.sqlite3")
        self.namespace = "autosign:" + username
        self.records.put("autosign", "owner", {"username": username})
        self.adapter_factory, self.notifications, self.clock = adapter_factory, notifications, clock
        self.lock = threading.Lock()
        self.submission_gate = threading.Lock()
        self.stopping = threading.Event()
        self.closing = threading.Event()
        self.notices = []
        self.query_count = 0
        self.control = self.records.get(self.namespace, "control", {"failures": 0})
        self.control.pop("enabled", None)
        self.config = validate_config(self.control.get("config"))

    def restore(self):
        """Called after App has explicitly registered the task function."""
        schedule_ids = {
            row["id"] for row in self.daemon.schedules.list()
        }
        if self.TASK_ID not in schedule_ids:
            self.daemon.schedules.create_internal(
                "autosign.check",
                {},
                self.config["interval"],
                schedule_id=self.TASK_ID,
                resume_on_restart=True,
            )
        else:
            self.daemon.schedules.set_resume_on_restart(
                self.TASK_ID, True)

    def _schedule_status(self):
        row = self.daemon.schedules.get(self.TASK_ID)
        task_id = row["current_task_id"]
        running = bool(
            task_id and self.daemon.tasks.get(task_id)["state"]
            in {"queued", "running"}
        )
        return {
            "version": row["version"],
            "enabled": row["enabled"],
            "running": running,
            "next_run": time.monotonic()
            + max(0, row["next_run_at"] - time.time()),
        }

    def _save_control(self):
        self.control.pop("enabled", None)
        self.control["config"] = self.config
        self.records.put(self.namespace, "control", self.control)

    def start(self, config=None):
        with self.lock, self.submission_gate:
            if self.closing.is_set():
                raise InputError("后台正在停止，无法启用签到")
            if self._schedule_status()["version"] != 1:
                raise ConfigError("未知 autosign 任务版本，无法恢复")
            if config is not None:
                if not isinstance(config, dict):
                    raise ConfigError("autosign 配置必须为 JSON 对象")
                value = {**self.config, **config}
                if isinstance(config.get("radar"), dict):
                    value["radar"] = {**self.config["radar"], **config["radar"]}
                self.config = validate_config(value)
            self.stopping.clear()
            self.daemon.schedules.set_interval(
                self.TASK_ID, self.config["interval"])
            self._save_control()
            self.daemon.schedules.enable(self.TASK_ID)
        return self.status()

    def stop(self):
        # Set before acquiring the gate: a following radar/code write must not start.
        self.stopping.set()
        with self.submission_gate:
            self._save_control()
            self.daemon.schedules.disable(self.TASK_ID)
        if not self.closing.is_set():
            self.stopping.clear()
        return self.status()

    def shutdown(self):
        self.closing.set()
        self.stopping.set()  # Preserve enabled intent across a daemon restart.

    def status(self):
        status = self._schedule_status()
        delay = max(0, status["next_run"] - time.monotonic())
        return {"enabled": status["enabled"], "running": status["running"] or self.lock.locked(),
            "last_successful_check": self.control.get("last_successful_check"),
            "next_check": (datetime.fromisoformat(self.clock()) + timedelta(seconds=delay)).isoformat()
                if status["enabled"] and not self.closing.is_set() else None,
            "error": self.control.get("error"), "failures": self.control.get("failures",0),
            "interval": self.config["interval"]}

    def activities(self):
        return {"activities": [value for key,value in self.records.items(self.namespace) if key != "control"]}

    def history(self, date=None):
        return {"query_count": self.query_count, "events": [e for e in self.records.events(self.namespace)
            if date is None or e["confirmed_at"].startswith(date)]}

    def _notice(self, key, message):
        notice = {"event_id": str(uuid.uuid5(uuid.NAMESPACE_URL,self.namespace+":"+key)), "message": message}
        self.records.put(self.namespace+":notices",notice["event_id"],notice)
        self.notices.append(notice)

    def _publish(self):
        if self.notifications:
            try:
                self.notifications.publish(self.notices)
            except Exception:
                # Delivery is best effort and can never change attendance outcome.
                pass
        self.notices = []

    def _transition(self, record, state, *, source=None, reason=None):
        record = {**record, "state": state, "updated_at": self.clock()}
        if reason:
            record["reason"] = reason
        else:
            record.pop("reason", None)
        event = None
        if state in {"success", "ended"}:
            record["source"] = source
            record["confirmed_at"] = self.clock()
            event = {key: record.get(key) for key in ("confirmed_at", "course", "title", "activity_id", "type", "source")}
            event.update(event_id=str(uuid.uuid5(uuid.NAMESPACE_URL,
                self.namespace+":"+record["activity_id"]+":"+state)), final_status=state)
        self.records.put(self.namespace, record["activity_id"], record, event=event)
        if state in {"waiting_input", "success"} or reason == "unknown_type":
            label = {"waiting_input": "需要输入签到码", "success": "签到完成"}.get(state, "未知签到类型")
            self._notice(record["activity_id"]+":"+state,
                f"{label}：{record['course']} / {record['title']}（活动 {record['activity_id']}）")
        return record

    def _discover(self):
        adapter = self.adapter_factory()
        try:
            rows = adapter.activities()
        except SessionExpired:
            adapter = self.adapter_factory()
            rows = adapter.activities()
        return adapter, rows

    def check(self, *, read_only=False):
        with self.lock:
            if not read_only:
                if self.closing.is_set() or self.stopping.is_set():
                    return NextRun(self.config["interval"])
                if not self._schedule_status()["enabled"]:
                    return NextRun(self.config["interval"])
            self.query_count += 1
            try:
                adapter, rows = self._discover()
                for row in rows:
                    self._process(adapter, row, read_only=read_only)
                if self.control.get("failures",0) >= 3:
                    self._notice("recovered:"+str(self.control.get("fault_started")), "自动签到检查已恢复正常")
                self.control.update(last_successful_check=self.clock(), failures=0, error=None)
                self._save_control()
                return NextRun(self.config["interval"])
            except Exception as exc:
                failures = self.control.get("failures",0)+1
                if failures == 1:
                    self.control["fault_started"] = self.clock()
                error = "authentication_failed" if isinstance(exc,AuthenticationError) else (
                    "configuration_error" if isinstance(exc,ConfigError) else "query_failed")
                self.control.update(failures=failures, error=error)
                self._save_control()
                if failures >= 3:
                    self._notice("fault:"+self.control["fault_started"], "自动签到持续检查失败："+error)
                return NextRun(min(60,4*2**min(failures-1,4)))
            finally:
                self._publish()


    def _process(self, adapter, row, *, read_only):
        activity_id = str(row.get("rollcall_id", ""))
        if not re.fullmatch(r"[0-9]+",activity_id):
            raise InputError("签到列表包含无效活动 ID")
        existing = self.records.get(self.namespace, activity_id)
        if existing and existing["state"] in {"success", "ended"}:
            return
        record = existing or {"activity_id": activity_id, "course": str(row.get("course_title") or "")[:200],
            "title": str(row.get("title") or "")[:200],
            "type": "radar" if row.get("is_radar") else "number" if row.get("is_number") else "unknown",
            "discovered_at": self.clock(), "state": "discovered"}
        if completed(row):
            source = "tool" if record["state"] in {"processing", "pending_verification"} else "platform_existing"
            self._transition(record,"success",source=source)
            return
        if row.get("is_expired") or row.get("rollcall_status") not in {"in_progress", None}:
            self._transition(record,"ended",reason="platform_ended")
            return
        # A lost response / crash must never turn into an implicit second write.
        if record["state"] in {"processing","pending_verification"}:
            self._verify(adapter,record)
            return
        if record["type"] == "unknown":
            if not existing:
                self._transition(record,"discovered",reason="unknown_type")
            return
        if not existing:
            record = self._transition(record,"discovered")
        if read_only or self.stopping.is_set():
            return
        if record["state"] == "waiting_input":
            return
        if record["type"] == "number":
            code = adapter.code(activity_id)
            if code is None:
                self._transition(record,"waiting_input",reason="code_unavailable")
            else:
                outcome, record = self._submit(adapter,record,lambda: adapter.number(activity_id,code))
                if outcome and outcome.get("rejected"):
                    self._transition(record,"waiting_input",reason="code_rejected")
        else:
            self._radar(adapter,record)

    def _verify(self, adapter, record):
        try:
            if adapter.confirmed(record["activity_id"]):
                return self._transition(record,"success",source="tool")
        except Exception:
            self._transition(record,"pending_verification",reason="result_unconfirmed")
            # A failed status read participates in query backoff; the write is never retried.
            raise
        return self._transition(record,"pending_verification",reason="result_unconfirmed")

    def _submit(self, adapter, record, write):
        with self.submission_gate:
            if (self.closing.is_set() or self.stopping.is_set()
                    or not self._schedule_status()["enabled"]):
                return None,record
            previous = record
            record = self._transition(record,"processing")
            if self.closing.is_set() or self.stopping.is_set():
                self.records.put(self.namespace,previous["activity_id"],previous)
                return None,previous
            # The gate orders stop against sending a write. Once sent, always verify it.
            try:
                outcome = write()
            except Exception:
                return None,self._verify(adapter,record)
        if outcome.get("confirmed"):
            return outcome,self._transition(record,"success",source="tool")
        if not outcome.get("rejected"):
            return None,self._verify(adapter,record)
        return outcome,record

    def _radar(self, adapter, record):
        radar = self.config["radar"]
        points=[]
        for point in ([radar["location"]] if radar["location"] else []) + radar["candidates"]:
            if point not in points:
                points.append(point)
        samples=[]
        count=0
        for point in points:
            if count >= radar["max_attempts"] or self.stopping.is_set():
                break
            outcome,record = self._submit(adapter,record,lambda: adapter.radar(record["activity_id"],point))
            count+=1
            if outcome is None or record["state"] == "success":
                return
            if not outcome.get("out_of_scope"):
                self._transition(record,"ended",reason="radar_rejected")
                return
            d=outcome.get("distance")
            if isinstance(d,str):
                try: d=float(d)
                except ValueError: d=None
            samples.append((point,d))
        estimate = estimate_position(samples) if radar["estimate"] and not self.stopping.is_set() else None
        if estimate and count < radar["max_attempts"]:
            outcome,record=self._submit(adapter,record,lambda: adapter.radar(record["activity_id"],estimate))
            if outcome is None or record["state"] == "success":
                return
        if not self.stopping.is_set():
            self._transition(record,"ended",reason="radar_attempts_exhausted")

    def answer(self, activity_id, code):
        if not isinstance(code,str) or not re.fullmatch(r"[0-9]{4}",code):
            raise InputError("签到码必须是四位数字")
        with self.lock:
            if (not self._schedule_status()["enabled"]
                    or self.stopping.is_set()):
                raise InputError("请先显式开启 autosign")
            record=self.records.get(self.namespace,str(activity_id))
            if not record or record["type"] != "number" or record["state"] != "waiting_input":
                raise InputError("活动当前不在等待数字签到码状态")
            try:
                adapter,rows=self._discover()
                row=next((r for r in rows if str(r.get("rollcall_id")) == str(activity_id)),None)
                if row is None:
                    return self._transition(record,"ended",reason="platform_not_active")
                self._process(adapter,row,read_only=True)
                record=self.records.get(self.namespace,str(activity_id))
                if record["state"] != "waiting_input":
                    return record
                outcome,record=self._submit(adapter,record,lambda: adapter.number(activity_id,code))
                if outcome and outcome.get("rejected"):
                    record=self._transition(record,"waiting_input",reason="code_rejected")
                return record
            finally:
                self._publish()


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
                    if os.name != "nt":
                        path.chmod(0o600)
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
