"""Module container and daemon lifecycle."""
from pathlib import Path
import logging
import threading
import time

from zju_cli.shared.errors import InputError, RemoteError
from zju_cli.backend.command_registry import CommandRegistry
from zju_cli.backend.modules.autosign import AutosignModule
from zju_cli.backend.modules.classroom import ClassroomModule
from zju_cli.backend.modules.courses import CoursesModule
from zju_cli.backend.modules.schedules import SchedulesModule
from zju_cli.backend.modules.tasks import TasksModule, current_task
from zju_cli.backend.modules.zjuam import ZjuamModule
from zju_cli.backend.modules.webvpn import WebVPNModule
from zju_cli.backend.modules.cc98 import Cc98Module
from zju_cli.shared.storage import default_data_dir

logger = logging.getLogger(__name__)


class Daemon:
    def __init__(self, config, *, auth=None, state_dir=None):
        self.command_registry = CommandRegistry()
        self.config = config
        self.state_dir = Path(state_dir) if state_dir else default_data_dir()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.accepting = False
        self._closed = False
        self._modules = []
        try:
            self._attach("tasks", TasksModule)
            self._attach("schedules", SchedulesModule)
            self._attach("zjuam", lambda daemon, config: ZjuamModule(daemon, config, auth=auth))
            self._attach("webvpn", WebVPNModule)
            self._attach("courses", CoursesModule)
            self._attach("classroom", ClassroomModule)
            self._attach("autosign", AutosignModule)
            self._attach("cc98", Cc98Module)
            self.command_registry.register("daemon.status", self.status)
            self.command_registry.register("daemon.stop", self.stop_command)
            self.command_registry.register(
                "command.list", self.command_registry.command_list)
            self.autosign.restore()
            self.schedules.restore()
            self.command_registry.close_registration()
            self.accepting = True
            self.schedules.start()
            logger.info("daemon modules started")
        except BaseException:
            self.close()
            raise

    def _attach(self, name, constructor):
        module = constructor(self, self.config)
        setattr(self, name, module)
        self._modules.append(module)

    @property
    def task(self):
        context = current_task.get()
        if context is None:
            raise RuntimeError("当前没有运行中的任务")
        return context

    def report_progress(self, message):
        logger.debug("task progress")

    def _authorize(self, username):
        if username != self.config.username:
            raise RemoteError("后台绑定其他主账号，请先停止后台")

    def handle_rpc(self, message):
        """Route one authenticated RPC message and report whether to stop serving."""
        if not isinstance(message, dict):
            raise InputError("后台请求格式无效")
        action = message.get("action")
        parameters = message.get("parameters", {})
        shutdown = False
        if action == "stop":
            self.begin_stop()
            result = {"running": False}
            shutdown = True
        elif action == "submit":
            task_id = self.tasks.submit_request(
                message.get("command"),
                parameters,
                username=message.get("username"),
                timeout=message.get("timeout"),
                artifact_root=message.get("artifact_root"),
            )
            result = {"task_id": task_id}
        elif action == "command":
            command = message.get("command")
            username = message.get("username")
            if username is None and command in {
                    "zjuam.status", "webvpn.status", "daemon.status",
                    "daemon.stop", "command.list", "autosign.status",
                    "autosign.list", "autosign.history", "autosign.stop"}:
                username = self.config.username
            self._authorize(username)
            result = self.command_registry.dispatch(command, parameters)
            shutdown = command == "daemon.stop"
        elif action == "command.list":
            result = self.command_registry.command_list()
        elif action == "status":
            result = self.status()
        else:
            raise RemoteError("未知后台操作")
        return result, shutdown

    def status(self):
        return {"running": True, "accepting": self.accepting}

    def stop_command(self):
        self.begin_stop()
        return {"running": False}

    def begin_stop(self):
        with self.lock:
            self.accepting = False
        schedules = getattr(self, "schedules", None)
        if schedules:
            schedules.stopping.set()
        tasks = getattr(self, "tasks", None)
        if tasks:
            tasks.begin_stop()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.begin_stop()
        tasks = getattr(self, "tasks", None)
        if tasks:
            tasks.close()
        for module in reversed(self._modules):
            try:
                module.close()
            except Exception as exc:
                logger.error("module close failed reason=%s", type(exc).__name__)
