"""One serialized attendance round. All writes are preceded by durable intent."""
from datetime import datetime, timedelta, timezone
import re
import threading
import uuid

from ..adapters.attendance import completed
from ..errors import AuthenticationError, ConfigError, InputError, SessionExpired
from ..scheduler import NextRun
from .attendance_config import validate_config, estimate_position

SHANGHAI = timezone(timedelta(hours=8))


def now_text():
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


class AttendanceService:
    TASK_ID = "autosign"

    def __init__(self, jobs, username, adapter_factory, *, notifications=None, clock=now_text):
        self.jobs, self.username = jobs, username
        self.records = jobs.records
        self.namespace = "autosign:" + username
        self.records.put("autosign", "owner", {"username": username})
        self.adapter_factory, self.notifications, self.clock = adapter_factory, notifications, clock
        self.lock = threading.Lock()
        self.submission_gate = threading.Lock()
        self.stopping = threading.Event()
        self.closing = threading.Event()
        self.notices = []
        self.query_count = 0
        self.control = self.records.get(self.namespace, "control", {"enabled": False, "failures": 0})
        self.config = validate_config(self.control.get("config"))

    def restore(self):
        """Called after App has explicitly registered the task function."""
        if self.TASK_ID not in self.jobs.periodic.tasks:
            self.jobs.create_periodic(self.TASK_ID, "autosign", {}, self.config["interval"], resume_on_restart=True)
        # Both durable account identity and explicit intent must match before enabling.
        if self.control.get("enabled"):
            try:
                self.jobs.enable_periodic(self.TASK_ID)
            except ValueError:
                self.jobs.pause_periodic(self.TASK_ID)
                self.control["error"] = "unknown_task_version"
                self._save_control()
        else:
            self.jobs.pause_periodic(self.TASK_ID)

    def _save_control(self):
        self.control["config"] = self.config
        self.records.put(self.namespace, "control", self.control)

    def start(self, config=None):
        with self.lock, self.submission_gate:
            if self.closing.is_set():
                raise InputError("后台正在停止，无法启用签到")
            if self.jobs.periodic_status(self.TASK_ID)["version"] != 1:
                raise ConfigError("未知 autosign 任务版本，无法恢复")
            if config is not None:
                if not isinstance(config, dict):
                    raise ConfigError("autosign 配置必须为 JSON 对象")
                value = {**self.config, **config}
                if isinstance(config.get("radar"), dict):
                    value["radar"] = {**self.config["radar"], **config["radar"]}
                self.config = validate_config(value)
            self.control["enabled"] = True
            self.stopping.clear()
            self.jobs.periodic.configure(self.TASK_ID, interval=self.config["interval"])
            self._save_control()
            self.jobs.enable_periodic(self.TASK_ID)
        return self.status()

    def stop(self):
        # Set before acquiring the gate: a following radar/code write must not start.
        self.stopping.set()
        with self.submission_gate:
            self.control["enabled"] = False
            self._save_control()
            self.jobs.pause_periodic(self.TASK_ID)
        return self.status()

    def shutdown(self):
        self.closing.set()
        self.stopping.set()  # Preserve enabled intent across a daemon restart.

    def status(self):
        status = self.jobs.periodic_status(self.TASK_ID)
        delay = max(0, status["next_run"] - self.jobs.periodic.clock())
        return {"enabled": bool(self.control.get("enabled")), "running": status["running"] or self.lock.locked(),
            "last_successful_check": self.control.get("last_successful_check"),
            "next_check": (datetime.fromisoformat(self.clock()) + timedelta(seconds=delay)).isoformat()
                if status["enabled"] and self.control.get("enabled") and not self.stopping.is_set() else None,
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
            if not read_only and (not self.control.get("enabled") or self.stopping.is_set()):
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
            if self.closing.is_set() or self.stopping.is_set() or not self.control.get("enabled"):
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
            if not self.control.get("enabled") or self.stopping.is_set():
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

def offline_view(command, parameters=None):
    """Local queries and disabling do not start a daemon or require credentials."""
    from ..runtime import runtime_dir
    from ..job_manager import JobRecords
    database = runtime_dir() / "scheduler.sqlite3"
    empty = {"enabled": False, "running": False, "last_successful_check": None,
             "next_check": None, "error": None}
    if not database.is_file():
        return {"query_count": 0, "events": []} if command == "history" else {"activities": []} if command == "list" else empty
    records = JobRecords(database)
    owner = records.get("autosign", "owner")
    if not owner:
        return {"query_count": 0, "events": []} if command == "history" else {"activities": []} if command == "list" else empty
    namespace = "autosign:" + owner["username"]
    control = records.get(namespace,"control",{})
    if command == "stop":
        control["enabled"] = False
        records.put(namespace,"control",control)
        records.disable_periodic("autosign")
    if command == "history":
        date = (parameters or {}).get("date")
        return {"query_count": 0, "events": [e for e in records.events(namespace) if date is None or e["confirmed_at"].startswith(date)]}
    if command == "list":
        return {"activities": [v for k,v in records.items(namespace) if k != "control"]}
    return {**empty, "enabled": bool(control.get("enabled")),
        "last_successful_check": control.get("last_successful_check"), "error": control.get("error"),
        "failures": control.get("failures",0), "daemon_running": False}
