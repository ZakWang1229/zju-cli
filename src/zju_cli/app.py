"""Explicit business composition root for the local daemon."""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import threading

from .auth_manager import AuthManager
from .errors import RemoteError
from .services.platform import ZjuService
from .job_manager import JobManager
from .scheduler import PeriodicScheduler
from .services.audio_task import AudioTask
from .services.audio_jobs import AudioJobService
from .storage import JobStore
from .runtime import runtime_dir


class BackgroundApp:
    def __init__(self, settings, *, auth=None, service_factory=ZjuService):
        self.settings = settings
        self.auth = auth or AuthManager(settings)
        self.service_factory = service_factory
        self.lock = threading.Lock()
        self.accepting = True
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="business")
        self.slots = threading.BoundedSemaphore(16)
        self.operations = {}
        self.jobs = JobManager(periodic_database=runtime_dir() / "scheduler.sqlite3")
        self.periodic = self.jobs.periodic
        self.jobs.register("class-audio", 1, AudioTask(
            lambda parameters: self._service(parameters.get("http_timeout"))))
        self.scheduler_stop = threading.Event()
        self.scheduler_thread = threading.Thread(target=self._schedule, daemon=True)
        for name in ("todos", "courses", "homework", "submit_homework", "retract_homework", "schedule"):
            self.register(name, lambda service, parameters, method=name: getattr(service, method)(**parameters))
        from .adapters.attendance import AttendanceAdapter
        from .services.attendance import AttendanceService
        from .services.attendance_notifications import AttendanceNotifications
        self.autosign = AttendanceService(self.jobs, settings.username,
            lambda: AttendanceAdapter(self.auth.courses(settings.timeout_seconds)))
        self.jobs.register_periodic("autosign", self.autosign.check, read_only=False)
        self.autosign.restore()
        self.autosign_notifications = AttendanceNotifications(self.jobs.records, self.autosign.namespace,
            runtime_dir(), lambda: self.autosign.config)
        self.autosign.notifications = self.autosign_notifications
        self.autosign_notifications.publish()
        self.scheduler_thread.start()

    def autosign_operation(self, command, parameters, *, username=None):
        read_commands = {"status", "list", "history", "stop"}
        if username is not None and username != self.settings.username:
            raise RemoteError("后台绑定其他主账号")
        if command not in read_commands and username != self.settings.username:
            raise RemoteError("签到操作需要主账号")
        if command == "status":
            return self.autosign.status()
        if command == "list":
            return self.autosign.activities()
        if command == "history":
            return self.autosign.history(parameters.get("date"))
        if command == "stop":
            return self.autosign.stop()
        operations = {"start": lambda: self.autosign.start(parameters.get("config")),
            "answer": lambda: self.autosign.answer(parameters.get("activity_id"),parameters.get("code")),
            "discover": lambda: self._autosign_discover()}
        if command not in operations:
            raise RemoteError("未知签到操作")
        with self.lock:
            if not self.accepting or not self.slots.acquire(blocking=False):
                raise RemoteError("后台正在停止或请求队列已满")
            try:
                future = self.executor.submit(operations[command])
            except BaseException:
                self.slots.release()
                raise
        future.add_done_callback(lambda _: self.slots.release())
        return future.result()

    def _autosign_discover(self):
        self.autosign.check(read_only=True)
        status = self.autosign.status()
        return {**self.autosign.activities(), **status, "ok": status["error"] is None}

    def _schedule(self):
        while not self.scheduler_stop.wait(0.2):
            self.periodic.tick()

    def resume_job(self, root, job_id, *, username):
        if username != self.settings.username:
            raise RemoteError("后台绑定其他主账号")
        job = JobStore(root).get(job_id)
        if job.get("username") != username:
            raise RemoteError("任务属于其他账号")
        with self.lock:
            if not self.accepting:
                raise RemoteError("后台正在停止")
            self.jobs.resume(root, job_id)
        return JobStore(root).get(job_id)

    def submit_audio(self, root, parameters, *, username, timeout=None):
        if username != self.settings.username:
            raise RemoteError("后台绑定其他主账号")
        settings = replace(self.settings, timeout_seconds=timeout or self.settings.timeout_seconds)
        store = JobStore(root)
        service = AudioJobService(store, create=lambda document: self.jobs.create(root, document))
        with self.lock:
            if not self.accepting:
                raise RemoteError("后台正在停止")
            result = service.submit(**parameters, settings=settings, launch=False)
            self.jobs.resume(root, result["job_id"])
        return result

    def cancel_job(self, root, job_id):
        if JobStore(root).get(job_id).get("username") != self.settings.username:
            raise RemoteError("任务属于其他账号")
        return self.jobs.cancel(root, job_id)

    def register(self, name, operation):
        if name in self.operations:
            raise ValueError("Operation already registered")
        self.operations[name] = operation

    def status(self):
        with self.lock:
            return {"running": True, "accepting": self.accepting}

    def dispatch(self, name, parameters, *, username, timeout=None):
        if username != self.settings.username:
            raise RemoteError("后台绑定其他主账号，请先停止后台")
        if name not in self.operations or not isinstance(parameters, dict):
            raise RemoteError("未知业务操作或无效参数")
        with self.lock:
            if not self.accepting:
                raise RemoteError("后台正在停止")
            if not self.slots.acquire(blocking=False):
                raise RemoteError("后台请求队列已满")
            try:
                future = self.executor.submit(self._execute, name, parameters, timeout)
            except BaseException:
                self.slots.release()
                raise
        future.add_done_callback(lambda _: self.slots.release())
        return future.result()

    def _execute(self, name, parameters, timeout):
        service = self._service(timeout)
        if name == "submit_homework":
            from pathlib import Path
            parameters = {**parameters, "file_path": Path(parameters["file_path"])}
        return self.operations[name](service, parameters)

    def _service(self, timeout):
        settings = replace(self.settings, timeout_seconds=(
            self.settings.timeout_seconds if timeout is None else timeout))
        manager = self.auth

        class OperationAuth:
            def courses(self):
                return manager.courses(settings.timeout_seconds)

            def cmc(self):
                return manager.cmc(settings.timeout_seconds)

        return self.service_factory(settings, auth=OperationAuth())

    def begin_stop(self):
        with self.lock:
            self.accepting = False
            self.autosign.shutdown()
            self.scheduler_stop.set()
            self.jobs.stopping.set()

    def close(self):
        self.begin_stop()
        self.scheduler_thread.join()
        self.jobs.close()
        self.executor.shutdown(wait=True)
        self.autosign_notifications.close()
