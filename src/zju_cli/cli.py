from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

from . import __version__
from .config import Settings
from .errors import InputError, ZjuError
from .output import emit, log
from .services.platform import ZjuService
from .services.audio_jobs import AudioJobService, summary
from .storage import JobStore, TERMINAL, default_data_dir


def common_options(parser, *, root=False):
    default = None if root else argparse.SUPPRESS
    parser.add_argument("--json", action="store_true", default=default, help="输出 JSON")
    parser.add_argument("--quiet", action="store_true", default=default, help="隐藏进度日志")
    parser.add_argument("--config", type=Path, default=default, help="指定 .env 配置文件")
    parser.add_argument("--http-timeout", type=float, default=default, help="单次 HTTP 请求超时（秒，默认 30）")
    parser.add_argument("--data-dir", type=Path, default=default, help="后台音频任务存储目录")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="zju", description="浙江大学学习平台 CLI")
    root.add_argument("--version", action="version", version=f"zju {__version__}")
    common_options(root, root=True)
    shared = argparse.ArgumentParser(add_help=False)
    common_options(shared)
    commands = root.add_subparsers(dest="command", required=True)
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


def wait_audio_job(store, job_id, args):
    validate_wait_timeout(args.wait_timeout)
    started = time.monotonic()
    last_report = -math.inf
    while True:
        job = store.get(job_id)
        if job["status"] in TERMINAL:
            if job["status"] == "succeeded":
                result = AudioJobService(store).result(job_id)
                return result, 5 if result.get("output_error") else 0
            return summary(job), job.get("error_exit_code") or 4
        if job["status"] == "paused":
            return summary(job), job.get("error_exit_code") or 4
        now = time.monotonic()
        if args.wait_timeout is not None and now - started >= args.wait_timeout:
            return {**summary(job), "wait_timed_out": True, "note": "后台音频任务继续运行"}, 7
        if not args.quiet and now - last_report >= 5:
            view = summary(job)
            position = (job.get("progress") or {}).get("media_position", "-")
            log(f"job_id={job_id}；{job['status']}/{job['stage']}；已用 {view['elapsed_seconds']} 秒；媒体位置 {position}")
            last_report = now
        time.sleep(0.5)


def run_audio(args):
    if args.audio_command == "extract":
        validate_wait_timeout(args.wait_timeout)
    store = JobStore(args.data_dir or Path(os.environ.get("ZJU_DATA_DIR") or default_data_dir()))
    service = AudioJobService(store)
    if args.audio_command in {"submit", "extract"}:
        settings = Settings.load(args.config, args.http_timeout)
        result = service.submit(args.sub_id, args.course_id, args.out, settings=settings,
            config_path=args.config, overwrite=args.overwrite, ffmpeg_bin=args.ffmpeg_bin, timeout=args.timeout)
        if args.audio_command == "submit" or args.detach:
            return result
        if not args.quiet:
            log(f"job_id={result['job_id']}；Ctrl-C 仅停止等待，后台任务继续")
        return wait_audio_job(store, result["job_id"], args)
    if args.job_command == "list":
        if not 1 <= args.limit <= 1000:
            raise InputError("--limit 必须在 1 到 1000 之间")
        return {"jobs": [summary(job) for job in store.list(args.limit)]}
    if args.job_command == "status":
        return summary(store.get(args.job_id))
    if args.job_command == "wait":
        return wait_audio_job(store, args.job_id, args)
    if args.job_command == "result":
        return service.result(args.job_id, out=args.out, overwrite=args.overwrite)
    if args.job_command == "resume":
        return service.resume(args.job_id, config_path=args.config)
    return summary(store.cancel(args.job_id))


def confirm_write(args):
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
            code = 5 if result.get("output_error") else (4 if result.get("ok") is False else 0)
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
        else:
            log("已中断")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
