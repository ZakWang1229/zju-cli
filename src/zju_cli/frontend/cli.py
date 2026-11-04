from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

from zju_cli import __version__
from zju_cli.shared.config import Settings
from zju_cli.shared.errors import InputError, ZjuError
from zju_cli.frontend.output import emit, log
from zju_cli.frontend.remote_service import RemoteService as ZjuService
from zju_cli.shared.storage import default_data_dir
from zju_cli.frontend.rpc import DaemonClient


def common_options(parser, *, root=False):
    default = None if root else argparse.SUPPRESS
    parser.add_argument("--json", action="store_true", default=default, help="输出 JSON")
    parser.add_argument("--quiet", action="store_true", default=default, help="隐藏进度日志")
    parser.add_argument("--config", type=Path, default=default, help="指定 .env 配置文件")
    parser.add_argument("--http-timeout", type=float, default=default, help="单次 HTTP 请求超时（秒，默认 30）")
    parser.add_argument("--data-dir", type=Path, default=default,
                        help="任务产物目录；不改变后台状态库位置")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="zju", description="浙江大学学习平台 CLI")
    root.add_argument("--version", action="version", version=f"zju {__version__}")
    common_options(root, root=True)
    shared = argparse.ArgumentParser(add_help=False)
    common_options(shared)
    commands = root.add_subparsers(dest="command", required=True)
    session = commands.add_parser("daemon", aliases=["session"], parents=[shared], help="统一后台管理")
    session.add_argument("session_command", choices=["status", "start", "restart", "stop", "run", "autostart"])
    session.add_argument("autostart_action", nargs="?", choices=["enable", "disable", "status"], default="status")
    task = commands.add_parser("task", parents=[shared], help="后台任务管理")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    task_submit = task_commands.add_parser("submit", parents=[shared])
    task_submit.add_argument("target_command")
    task_submit.add_argument("--parameters", default="{}", help="JSON 对象")
    task_submit.add_argument("--yes", action="store_true", help="确认任务中的真实作业写入")
    for name in ("get", "wait", "cancel", "resume"):
        item = task_commands.add_parser(name, parents=[shared])
        item.add_argument("task_id")
        if name == "wait":
            item.add_argument("--wait-timeout", type=float)
    task_list = task_commands.add_parser("list", parents=[shared])
    task_list.add_argument("--command", dest="target_command")
    task_list.add_argument("--state")
    task_list.add_argument("--limit", type=int, default=20)
    schedule_admin = commands.add_parser("schedules", parents=[shared], help="周期计划管理")
    schedule_commands = schedule_admin.add_subparsers(dest="schedule_command", required=True)
    schedule_create = schedule_commands.add_parser("create", parents=[shared])
    schedule_create.add_argument("target_command")
    schedule_create.add_argument("--parameters", default="{}")
    schedule_create.add_argument("--interval", type=float, required=True)
    schedule_create.add_argument("--enabled", action="store_true")
    schedule_create.add_argument("--yes", action="store_true", help="确认计划中的真实作业写入")
    for name in ("get", "enable", "disable", "delete"):
        item = schedule_commands.add_parser(name, parents=[shared])
        item.add_argument("schedule_id")
    schedule_commands.add_parser("list", parents=[shared])
    materials = commands.add_parser("materials", parents=[shared], help="课程资料")
    material_commands = materials.add_subparsers(dest="materials_command", required=True)
    for name in ("list", "download", "sync"):
        item = material_commands.add_parser(name, parents=[shared])
        item.add_argument("--course-id", required=True)
        if name != "list":
            item.add_argument("--out", type=Path, required=True)
        if name == "download":
            item.add_argument("--file-id", action="append")
            item.add_argument("--overwrite", action="store_true")
    cc98 = commands.add_parser("cc98", parents=[shared], help="CC98 论坛")
    cc98_commands = cc98.add_subparsers(dest="cc98_command", required=True)
    for name in ("search", "posts", "hot", "new", "boards", "board", "user", "image", "download", "status"):
        item = cc98_commands.add_parser(name, parents=[shared])
        item.add_argument("--webvpn-mode", choices=["auto", "direct", "vpn"])
        if name in {"search", "posts", "hot", "new", "board", "user"}:
            item.add_argument("--from", dest="offset", type=int, default=0)
            item.add_argument("--size", type=int, default=10 if name == "posts" else 20)
        if name == "search":
            item.add_argument("keyword")
        if name in {"posts", "board"}:
            item.add_argument("id")
        if name == "board":
            item.add_argument("--best", action="store_true")
        if name == "hot":
            item.add_argument("--range", choices=["today", "weekly", "monthly"], default="today")
        if name == "user":
            group = item.add_mutually_exclusive_group(required=True)
            group.add_argument("--id")
            group.add_argument("--name")
        if name in {"image", "download"}:
            item.add_argument("url")
            item.add_argument("--dir", dest="directory", type=Path)
            item.add_argument("--filename")
    command_root = commands.add_parser("command", parents=[shared], help="后台命令注册表")
    command_root.add_subparsers(dest="command_action", required=True).add_parser("list", parents=[shared])
    for name in ("zjuam", "webvpn"):
        item = commands.add_parser(name, parents=[shared], help="本地认证状态")
        item.add_argument("auth_command", choices=["status"])
    autosign = commands.add_parser("autosign", parents=[shared], help="常驻自动签到（须显式开启）")
    sign_commands = autosign.add_subparsers(dest="autosign_command", required=True)
    sign_start = sign_commands.add_parser("start", parents=[shared])
    sign_start.add_argument("--settings", type=Path, help="导入集中签到 JSON 配置")
    sign_start.add_argument("--interval", type=float, help="每轮结束后的检查间隔秒数")
    for name in ("stop", "status", "list", "discover"):
        sign_commands.add_parser(name, parents=[shared])
    sign_answer = sign_commands.add_parser("answer", parents=[shared])
    sign_answer.add_argument("activity_id")
    sign_answer.add_argument("--code", required=True)
    sign_history = sign_commands.add_parser("history", parents=[shared])
    sign_history.add_argument("--date", help="按北京时间 YYYY-MM-DD 筛选")
    commands.add_parser("todos", parents=[shared], help="列出未完成待办")
    courses = commands.add_parser("courses", parents=[shared], help="列出课程")
    courses.add_argument("--keyword", default="")
    courses.add_argument("--status", action="append", choices=["ongoing", "notStarted", "ended"])
    homework = commands.add_parser("homework", parents=[shared], help="课程作业")
    homework_commands = homework.add_subparsers(dest="homework_command", required=True)
    homework_list = homework_commands.add_parser("list", parents=[shared], help="列出课程全部作业")
    homework_list.add_argument("--course-id", required=True)
    homework_list.add_argument("--no-submission", action="store_true", help="不读取提交详情")
    submit = homework_commands.add_parser("submit", parents=[shared], help="提交文件到作业")
    submit.add_argument("--activity-id", required=True)
    submit.add_argument("--file", type=Path, required=True)
    submit.add_argument("--comment", default="")
    submit.add_argument("--yes", action="store_true", help="确认真实提交")
    retract = homework_commands.add_parser("retract", parents=[shared], help="撤回已交作业")
    retract.add_argument("--activity-id", required=True)
    retract.add_argument("--yes", action="store_true", help="确认真实撤回")
    schedule = commands.add_parser("schedule", parents=[shared], help="查询课表，单次最多 7 天")
    schedule.add_argument("--date")
    schedule.add_argument("--end-date")
    audio = commands.add_parser("class-audio", parents=[shared], help="课程回放音频")
    audio_commands = audio.add_subparsers(dest="audio_command", required=True)
    for command in ("submit", "extract"):
        extraction = audio_commands.add_parser(command, parents=[shared],
            help="后台提交，立即返回 job_id" if command == "submit" else "提取音频并前台等待")
        extraction.add_argument("--sub-id", required=True)
        extraction.add_argument("--course-id", required=True)
        extraction.add_argument("--out", type=Path, required=True)
        extraction.add_argument("--ffmpeg-bin")
        extraction.add_argument("--overwrite", action="store_true")
        extraction.add_argument("--timeout", type=float, help="ffmpeg 总运行时间上限（秒，默认不限）")
        if command == "extract":
            extraction.add_argument("--detach", action="store_true", help="立即返回 job_id，后台继续")
            extraction.add_argument("--wait-timeout", type=float, help="仅限制本地等待（秒）")
    jobs = audio_commands.add_parser("jobs", parents=[shared], help="持久化音频任务管理")
    job_commands = jobs.add_subparsers(dest="job_command", required=True)
    listing = job_commands.add_parser("list", parents=[shared])
    listing.add_argument("--limit", type=int, default=20)
    for action in ("status", "wait", "result", "resume", "cancel"):
        job_parser = job_commands.add_parser(action, parents=[shared])
        job_parser.add_argument("job_id")
        if action == "wait":
            job_parser.add_argument("--wait-timeout", type=float)
        elif action == "result":
            job_parser.add_argument("--out", type=Path, help="导出已完成音频，无需重新下载")
            job_parser.add_argument("--overwrite", action="store_true")
    return root


def validate_wait_timeout(timeout):
    if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
        raise InputError("--wait-timeout 必须是有限的正数")


def wait_daemon_task(client, task_id, *, wait_timeout=None):
    validate_wait_timeout(wait_timeout)
    started = time.monotonic()
    while True:
        row = client.call({"action": "command", "command": "task.get", "parameters": {"task_id": task_id},
                           "username": client.settings.username}, start=False)
        if row["state"] == "succeeded":
            result = {"job_id": task_id, "task_id": task_id, **(row["result"] or {})}
            return result, 5 if result.get("output_error") else 0
        if row["state"] in {"failed", "cancelled", "interrupted"}:
            return row, (row.get("error") or {}).get("exit_code", 4)
        if wait_timeout is not None and time.monotonic() - started >= wait_timeout:
            return {"task_id": task_id, "job_id": task_id, "wait_timed_out": True}, 7
        time.sleep(0.1)


def run_audio(args):
    if args.audio_command == "extract":
        validate_wait_timeout(args.wait_timeout)
    store_root = Path(args.data_dir or os.environ.get("ZJU_DATA_DIR") or default_data_dir()).expanduser().resolve()
    if args.audio_command in {"submit", "extract"}:
        settings = Settings.load(args.config, args.http_timeout)
        binary = args.ffmpeg_bin or settings.ffmpeg_bin
        if os.path.dirname(binary):
            binary = str(Path(binary).expanduser().resolve())
        client = DaemonClient(settings)
        submitted = client.call({"action": "submit", "command": "classroom.audio.download",
            "artifact_root": str(store_root), "username": settings.username,
            "timeout": settings.timeout_seconds,
            "parameters": {"sub_id": args.sub_id, "course_id": args.course_id,
                "out": str(args.out.expanduser().resolve()), "overwrite": args.overwrite,
                "ffmpeg_bin": binary, "timeout": args.timeout}})
        result = {"task_id": submitted["task_id"], "job_id": submitted["task_id"]}
        if args.audio_command == "submit" or args.detach:
            return result
        if not args.quiet:
            log(f"job_id={result['job_id']}；Ctrl-C 仅停止等待，后台任务继续")
        return wait_daemon_task(client, result["task_id"], wait_timeout=args.wait_timeout)
    from zju_cli.frontend.task_store import read_tasks, audio_summary
    current = read_tasks(command="classroom.audio.download")

    if args.job_command == "list":
        if not 1 <= args.limit <= 1000:
            raise InputError("--limit 必须在 1 到 1000 之间")
        rows = [audio_summary(row) for row in current]
        return {"jobs": sorted(rows, key=lambda row: row["created_at"], reverse=True)[:args.limit]}

    task_rows = [row for row in current if row["id"] == args.job_id]
    if not task_rows:
        raise InputError("任务不存在")
    task = task_rows[0]

    if args.job_command == "status":
        return audio_summary(task), 0
    if args.job_command == "wait":
        if task["state"] == "succeeded":
            result = {"job_id": args.job_id, "task_id": args.job_id, **(task["result"] or {})}
            return result, 5 if result.get("output_error") else 0
        if task["state"] in {"failed", "cancelled", "interrupted"}:
            return audio_summary(task), (task.get("error") or {}).get("exit_code", 4)
        settings = Settings(task["username"], "")
        return wait_daemon_task(DaemonClient(settings), args.job_id, wait_timeout=args.wait_timeout)
    if args.job_command == "result":
        if args.out is None:
            if task["state"] != "succeeded":
                raise InputError("任务没有完整结果")
            return {"job_id": args.job_id, "task_id": args.job_id, **(task["result"] or {})}
        settings = Settings.load(args.config, args.http_timeout)
        return DaemonClient(settings).execute("classroom.audio.export", {
            "source_task_id": args.job_id,
            "out": str(args.out.expanduser().resolve()),
            "overwrite": args.overwrite,
        })

    if args.job_command == "resume":
        settings = Settings.load(args.config, args.http_timeout)
        client = DaemonClient(settings)
        result = client.call({"action": "command", "command": "task.resume",
            "parameters": {"task_id": args.job_id},
            "username": settings.username})
        adopted = read_tasks(task_id=args.job_id, command="classroom.audio.download")
        summary = audio_summary(adopted[0]) if adopted else {
            "task_id": result, "job_id": result}
        return summary, 0
    settings = Settings.load(args.config, args.http_timeout)
    client = DaemonClient(settings)
    result = client.call({"action": "command", "command": "task.cancel",
        "parameters": {"task_id": args.job_id},
        "username": settings.username}, start=False)
    return audio_summary(result), 0


def confirm_write(args):
    if args.command in {"task", "schedules"} and getattr(args, "target_command", None) in {
            "submit_homework", "retract_homework", "courses.homework.submit", "courses.homework.retract"}:
        if getattr(args, "yes", False):
            return
        if not sys.stdin.isatty():
            raise InputError("非交互式作业写入必须显式传入 --yes")
        print("确认执行作业写入任务或计划？[y/N] ", file=sys.stderr, end="", flush=True)
        if sys.stdin.readline().strip().lower() not in {"y", "yes"}:
            raise InputError("操作已取消")
        return
    if args.command != "homework" or args.homework_command not in {"submit", "retract"} or args.yes:
        return
    if not sys.stdin.isatty():
        raise InputError("非交互式提交或撤回必须显式传入 --yes")
    operation = "提交" if args.homework_command == "submit" else "撤回"
    detail = f"；文件：{args.file.expanduser().absolute()}" if operation == "提交" else ""
    log(f"将真实{operation}作业 {args.activity_id}{detail}")
    print("确认执行？[y/N] ", file=sys.stderr, end="", flush=True)
    if sys.stdin.readline().strip().lower() not in {"y", "yes"}:
        raise InputError("操作已取消")


def run(args):
    confirm_write(args)
    if args.command in {"materials", "cc98"}:
        settings = Settings.load(args.config, args.http_timeout)
        client = DaemonClient(settings)
        if args.command == "materials":
            name = args.materials_command
            values = {"course_id": args.course_id}
            if name != "list":
                values["out"] = str(args.out.expanduser().resolve())
            if name == "download":
                values.update(file_ids=args.file_id, overwrite=args.overwrite)
            return client.execute("courses.materials." + name, values, wait=name == "list")
        name = args.cc98_command
        fields = ("webvpn_mode", "offset", "size", "keyword", "id", "best", "range", "name", "url", "directory", "filename")
        values = {key: getattr(args, key) for key in fields if getattr(args, key, None) is not None}
        if "directory" in values:
            values["directory"] = str(values["directory"].expanduser().resolve())
        return client.execute("cc98." + name, values, wait=name not in {"image", "download"})
    if args.command == "autosign":
        return run_autosign(args)
    if args.command in {"daemon", "session"}:
        if args.session_command == "autostart":
            from zju_cli.frontend.autostart import configure
            return configure(args.autostart_action, config=args.config, timeout=args.http_timeout)
        if args.session_command == "run":
            settings = Settings.load(args.config, args.http_timeout)
            from zju_cli.backend.server import serve
            return serve(settings, handle_signals=True)
        if args.session_command in {"start", "restart"}:
            settings = Settings.load(args.config, args.http_timeout)
            client = DaemonClient(settings)
            return client.start() if args.session_command == "start" else client.restart()
        client = DaemonClient(Settings("", "", timeout_seconds=args.http_timeout or 30))
        return client.status() if args.session_command == "status" else client.stop()
    if args.command == "command":
        client = DaemonClient(Settings("", "", timeout_seconds=args.http_timeout or 30))
        if not client.status()["running"]:
            client = DaemonClient(Settings.load(args.config, args.http_timeout))
        return client.call({"action": "command.list"})
    if args.command in {"zjuam", "webvpn"}:
        client = DaemonClient(Settings("", "", timeout_seconds=args.http_timeout or 30))
        if not client.status()["running"]:
            return {"daemon_running": False, "authenticated": False}
        return client.call({"action": "command", "command": args.command + ".status",
                            "parameters": {}}, start=False)
    if args.command in {"task", "schedules"}:
        settings = Settings.load(args.config, args.http_timeout)
        client = DaemonClient(settings)
        if args.command == "task":
            action = args.task_command
            if action == "wait":
                validate_wait_timeout(args.wait_timeout)
                started = time.monotonic()
                while True:
                    task = client.call({"action": "command", "command": "task.get",
                        "parameters": {"task_id": args.task_id}, "username": settings.username})
                    if task.get("state") in {"succeeded", "failed", "cancelled", "interrupted"}:
                        return task
                    if args.wait_timeout is not None and time.monotonic() - started >= args.wait_timeout:
                        return {**task, "wait_timed_out": True}
                    time.sleep(0.25)
            parameters = {key: getattr(args, key) for key in ("task_id", "command", "state", "limit")
                          if hasattr(args, key) and getattr(args, key) is not None}
            parameters.pop("command", None)
            if getattr(args, "target_command", None) is not None:
                parameters["command"] = args.target_command
            if action == "submit":
                try:
                    parameters["parameters"] = json.loads(args.parameters)
                except ValueError as exc:
                    raise InputError("--parameters 必须是 JSON") from exc
                if args.data_dir is not None:
                    parameters["artifact_root"] = str(args.data_dir.expanduser().resolve())
            result = client.call({"action": "command", "command": "task." + action,
                "parameters": parameters, "username": settings.username})
            if action in {"submit", "resume"}:
                return {"task_id": result}
            if action == "cancel":
                return result, 0
            return {"tasks": result} if isinstance(result, list) else result
        action = args.schedule_command
        parameters = {"schedule_id": args.schedule_id} if hasattr(args, "schedule_id") else {}
        if action == "create":
            try:
                values = json.loads(args.parameters)
            except ValueError as exc:
                raise InputError("--parameters 必须是 JSON") from exc
            if not isinstance(values, dict):
                raise InputError("--parameters 必须是 JSON 对象")
            parameters = {"command": args.target_command, "parameters": values,
                          "interval": args.interval, "enabled": args.enabled}
        result = client.call({"action": "command", "command": "schedule." + action,
            "parameters": parameters, "username": settings.username})
        if action == "create":
            return {"schedule_id": result}
        return {"schedules": result} if isinstance(result, list) else result
    if args.command == "class-audio":
        return run_audio(args)
    service = ZjuService(Settings.load(args.config, args.http_timeout), on_progress=None if args.quiet else log)
    if args.command == "todos":
        return service.todos()
    if args.command == "courses":
        return service.courses(args.keyword, args.status)
    if args.command == "homework":
        if args.homework_command == "list":
            return service.homework(args.course_id, not args.no_submission)
        if args.homework_command == "submit":
            return service.submit_homework(args.activity_id, args.file, args.comment)
        return service.retract_homework(args.activity_id)
    if args.command == "schedule":
        return service.schedule(args.date, args.end_date)
    raise InputError("未知命令")


def run_autosign(args):
    command = args.autosign_command
    parameters = {}
    if command == "history" and args.date:
        from datetime import date
        try:
            if date.fromisoformat(args.date).isoformat() != args.date:
                raise ValueError
        except ValueError as exc:
            raise InputError("日期格式应为 YYYY-MM-DD") from exc
        parameters["date"] = args.date
    if command == "answer":
        parameters.update(activity_id=args.activity_id, code=args.code)
    if command == "start" and (args.settings is not None or args.interval is not None):
        from zju_cli.shared.attendance_config import validate_config
        value = {}
        if args.settings:
            try:
                value = json.loads(args.settings.expanduser().resolve().read_text(encoding="utf-8-sig"))
            except (OSError, ValueError) as exc:
                raise InputError("无法读取签到 JSON 配置") from exc
        if args.interval is not None:
            if not isinstance(value,dict):
                raise InputError("签到配置必须为 JSON 对象")
            value["interval"] = args.interval
        validate_config(value)
        parameters["config"] = value
    read_only = command in {"status", "list", "history", "stop"}
    settings = Settings("", "") if read_only else Settings.load(args.config, args.http_timeout)
    client = DaemonClient(settings)
    if read_only and not client.status()["running"]:
        from zju_cli.shared.attendance_state import offline_view
        return offline_view(command, parameters)
    if read_only:
        return client.call({
            "action": "command",
            "command": "autosign." + command,
            "parameters": parameters,
            "username": None,
        }, start=False)
    return client.execute("autosign." + command, parameters)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        returned = run(args)
        if isinstance(returned, tuple):
            result, code = returned
        else:
            result = returned
            code = 5 if result.get("output_error") else (
                7 if result.get("wait_timed_out") else
                4 if result.get("ok") is False or result.get("state", result.get("status")) in {"failed", "cancelled", "interrupted"} else 0)
        emit(result, as_json=args.json)
        return code
    except ZjuError as exc:
        if args.json:
            log(json.dumps({"error": {"type": type(exc).__name__, "message": str(exc), "exit_code": exc.exit_code}}, ensure_ascii=False))
        else:
            log(f"错误：{exc}")
        return exc.exit_code
    except KeyboardInterrupt:
        if args.command == "class-audio":
            log("已停止当前等待；后台任务继续运行，可用 class-audio jobs list/status 查询")
        elif args.command in {"task", "todos", "courses", "homework", "schedule", "materials", "cc98"}:
            log("已停止前台等待；已提交的任务继续运行，可用 task list 查询")
        else:
            log("已中断")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
