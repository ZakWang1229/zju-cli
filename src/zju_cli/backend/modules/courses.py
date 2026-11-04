import threading
import weakref

from zju_cli.shared.errors import SessionExpired
from zju_cli.backend.modules.tasks import current_task


class CoursesModule:
    def __init__(self, daemon, config):
        self.daemon = daemon
        self.config = config
        self._lock = threading.Lock()
        self._session = None
        self._local = threading.local()
        self._sessions = weakref.WeakSet()
        self.service = CoursesService(config, read=self._read,
            session=self._courses_session, on_progress=daemon.report_progress)
        for name, handler in {
            "todos": self.todos,
            "courses": self.courses,
            "homework": self.homework,
            "submit_homework": self.submit_homework,
            "retract_homework": self.retract_homework,
            "courses.todos": self.todos,
            "courses.list": self.courses,
            "courses.homework.list": self.homework,
            "courses.homework.submit": self.submit_homework,
            "courses.homework.retract": self.retract_homework,
        }.items():
            daemon.command_registry.register(
                name, handler,
                durable=name.endswith(("submit", "retract"))
                or name in {"submit_homework", "retract_homework"})
        self.materials = MaterialsService(config, self._read, check=lambda: daemon.task.check_cancelled())
        daemon.command_registry.register("courses.materials.list", self.materials_list)
        daemon.command_registry.register(
            "courses.materials.download", self.materials_download,
            mode="job", durable=True)
        daemon.command_registry.register(
            "courses.materials.sync", self.materials_sync,
            mode="job", durable=True)

    def _courses_session(self, *, refresh=False):
        context = current_task.get()
        timeout = context.http_timeout if context and hasattr(context, "http_timeout") else self.config.timeout_seconds
        if getattr(self._local, "session", None) is None or refresh or self._local.session.timeout != timeout:
            self._local.session = self.daemon.zjuam.courses(timeout)
            with self._lock:
                self._sessions.add(self._local.session)
        return self._local.session

    def _read(self, operation):
        try:
            return operation(self._courses_session())
        except SessionExpired:
            return operation(self._courses_session(refresh=True))

    def attendance_adapter(self):
        adapter = AttendanceAdapter(self.daemon.zjuam.courses(self.config.timeout_seconds))
        class Session:
            def __getattr__(self, name):
                operation = getattr(adapter, name)
                def invoke(*args, **kwargs):
                    context = current_task.get()
                    if context:
                        context.check_cancelled()
                    return operation(*args, **kwargs)
                return invoke
        return Session()

    def attendance_list(self, *, session=None):
        return AttendanceAdapter(session or self._courses_session()).activities()

    def attendance_details(self, activity_id, *, session):
        return AttendanceAdapter(session).details(activity_id)

    def attendance_submit_number(self, activity_id, code, *, session):
        return AttendanceAdapter(session).number(activity_id, code)

    def attendance_submit_radar(self, activity_id, point, *, session):
        return AttendanceAdapter(session).radar(activity_id, point)

    def materials_list(self, course_id):
        return self.materials.list(course_id)

    def materials_download(self, course_id, out, file_ids=None, overwrite=False):
        return self.materials.transfer(course_id, out, file_ids, overwrite)

    def materials_sync(self, course_id, out):
        return self.materials.transfer(course_id, out, sync=True)

    def todos(self):
        return self.service.todos()

    def courses(self, keyword="", statuses=None):
        return self.service.courses(keyword, statuses)

    def homework(self, course_id, include_submission=True):
        return self.service.homework(course_id, include_submission)

    def submit_homework(self, activity_id, file_path, comment=""):
        return self.service.submit_homework(activity_id, file_path, comment)

    def retract_homework(self, activity_id):
        return self.service.retract_homework(activity_id)

    def close(self):
        with self._lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        for session in sessions:
            session.close()



import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from zju_cli.backend.modules.zjuam import COURSES
from zju_cli.shared.http import HttpSession
from zju_cli.shared.errors import InputError, LocalFileError, RemoteError, SessionExpired
from zju_cli.shared.validation import identifier, required_list

SHANGHAI = timezone(timedelta(hours=8))
TODO_TYPES = {"homework": "作业", "exam": "考试", "quiz": "测验", "survey": "问卷"}


def shanghai_date(value) -> str | None:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, str) and not value.isdigit():
            instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=timezone.utc)
        else:
            instant = datetime.fromtimestamp(float(value), timezone.utc)
        return instant.astimezone(SHANGHAI).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


class CoursesService:
    def __init__(self, settings, *, read, session, on_progress):
        self.settings = settings
        self._read = read
        self._session = session
        self.on_progress = on_progress

    def _json(self, session, path, *, method="GET", body=None):
        return CoursesAdapter(session).json(session, path, method=method, body=body)

    def _paged(self, session, path, key):
        return CoursesAdapter(session).paged(session, path, key)

    def todos(self):
        return self._read(lambda session: CoursesAdapter(session).todos())

    def courses(self, keyword="", statuses=None):
        statuses = statuses or ["ongoing", "notStarted", "ended"]
        if not set(statuses) <= {"ongoing", "notStarted", "ended"}:
            raise InputError("课程状态无效")
        return self._read(lambda session: CoursesAdapter(session).courses(keyword, statuses))

    def _user_id(self, session, course_id):
        return HomeworkAdapter(session)._user_id(session, course_id)

    def _submission(self, session, activity_id, user_id):
        return HomeworkAdapter(session)._submission(session, activity_id, user_id)

    def homework(self, course_id, include_submission=True):
        course_id = identifier(course_id)
        return self._read(lambda session: HomeworkAdapter(session).homework(course_id, include_submission))

    @staticmethod
    def _submission_summary(submission):
        return HomeworkAdapter._submission_summary(submission)

    def submit_homework(self, activity_id, file_path, comment=""):
        activity_id = identifier(activity_id)
        source = Path(file_path).expanduser().absolute()
        try:
            content = source.read_bytes()
        except OSError as exc:
            raise LocalFileError(f"无法读取提交文件：{source}") from exc
        session = self._session()
        self.on_progress("预留上传文件 …")
        adapter = HomeworkAdapter(session)
        reserve = adapter.reserve_upload(source.name, len(content))
        self.on_progress(f"上传 {source.name}（{len(content)} bytes） …")
        adapter.upload_content(reserve, source.name, content, getattr(session, "timeout", self.settings.timeout_seconds))
        self.on_progress("提交作业 …")
        try:
            _, submitted = adapter.submit(activity_id, reserve["id"], comment)
        except (RemoteError, SessionExpired) as exc:
            raise RemoteError("提交请求未获得成功确认，结果可能已生效；请先查询作业状态，避免重复提交") from exc
        result = {"ok": True, "activity_id": activity_id, "file": source.name,
                  "upload_id": reserve["id"], "message": submitted.get("message") or "已提交"}
        try:
            user_id = reserve.get("created_by_id")
            if not user_id:
                raise RemoteError("上传结果缺少用户 ID")
            submission = self._submission(session, activity_id, user_id)
            if not submission:
                raise RemoteError("提交列表为空")
            result["submission"] = self._submission_summary(submission)
        except (RemoteError, SessionExpired):
            result["warnings"] = ["提交接口已成功，但读回确认失败；请查询作业状态"]
        return result

    def retract_homework(self, activity_id):
        activity_id = identifier(activity_id)
        def fetch(session):
            activity = HomeworkAdapter(session).activity(activity_id)
            course_id = activity.get("course_id")
            if not course_id:
                raise RemoteError("作业接口未返回课程 ID")
            user_id = self._user_id(session, course_id)
            return session, activity, self._submission(session, activity_id, user_id)
        session, activity, submission = self._read(fetch)
        if not submission:
            return {"ok": False, "activity_id": activity_id, "message": "该作业没有提交记录"}
        if submission.get("is_draft"):
            return {"ok": True, "activity_id": activity_id, "submission_id": submission["id"], "message": "当前已是草稿"}
        if submission.get("can_retract") is False or (activity.get("data") or {}).get("allow_retract") is False:
            return {"ok": False, "activity_id": activity_id, "submission_id": submission["id"], "message": "该作业不允许撤回"}
        try:
            _, result = HomeworkAdapter(session).retract(submission["id"])
        except (RemoteError, SessionExpired) as exc:
            raise RemoteError("撤回请求未获得成功确认；请先查询作业状态") from exc
        return {"ok": True, "activity_id": activity_id, "submission_id": submission["id"],
                "message": result.get("message") or "已撤回为草稿"}



import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

from zju_cli.backend.modules.zjuam import COURSES
from zju_cli.shared.errors import BusyError, InputError, LocalFileError, RemoteError, SessionExpired
from zju_cli.shared.files import staged_output
from zju_cli.shared.validation import identifier


def safe_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name)).strip(" .")
    return (name[:160].rstrip(" .") or "file")


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@contextmanager
def course_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    lock = (directory / ".materials.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            if os.fstat(lock.fileno()).st_size == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise BusyError("该课程目录正在下载或同步") from exc
        else:
            import fcntl
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise BusyError("该课程目录正在下载或同步") from exc
        yield
    finally:
        lock.close()


class MaterialsService:
    def __init__(self, settings, read, check=None):
        self.settings = settings
        self.read = read
        self.check = check or (lambda: None)

    def list(self, course_id):
        return self.read(lambda session: MaterialsAdapter(session).list(course_id))

    def transfer(self, course_id, out, file_ids=None, overwrite=False, sync=False):
        course_id = identifier(course_id)
        selected = None if file_ids is None else {identifier(value) for value in file_ids}
        directory = Path(out).expanduser().absolute() / ("course_" + course_id)
        try:
            with course_lock(directory):
                return self._transfer(course_id, directory, selected, overwrite, sync)
        except OSError as exc:
            raise LocalFileError(f"无法访问课程目录：{directory}") from exc

    def _transfer(self, course_id, directory, selected, overwrite, sync):
        manifest_path = directory / ".materials.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if (not isinstance(manifest, dict) or manifest.get("version") != 1 or
                    manifest.get("username") != self.settings.username or manifest.get("course_id") != course_id or
                    not isinstance(manifest.get("files"), dict)):
                    raise ValueError
            except (OSError, ValueError) as exc:
                raise LocalFileError("同步清单无效或属于其他账号或课程") from exc
        else:
            manifest = {"version": 1, "username": self.settings.username, "course_id": course_id, "files": {}}
        remote = self.list(course_id)["files"]
        result = {"ok": True, "course_id": course_id, "downloaded": [], "skipped": [], "failed": [], "conflicts": []}
        if selected is not None:
            for missing in sorted(selected - {item["id"] for item in remote}):
                result["failed"].append({"id": missing, "path": None, "error": "附件不属于此课程或不存在"})
        for item in remote:
            self.check()
            fid = item["id"]
            if selected is not None and fid not in selected:
                continue
            old = manifest["files"].get(fid)
            if old is not None and not isinstance(old, dict):
                raise LocalFileError("同步清单附件记录无效")
            filename = old.get("path") if old else f"{fid}_{safe_name(item['name'])}"
            if not isinstance(filename, str) or not filename.startswith(fid + "_") or Path(filename).name != filename or Path(filename).drive or filename in (".", "..") or "/" in filename or "\\" in filename or ":" in filename:
                raise LocalFileError("同步清单包含非法路径")
            target = directory / filename
            entry = {"id": fid, "path": str(target)}
            try:
                exists = os.path.lexists(target)
                if sync and exists:
                    if not old or target.is_symlink() or not target.is_file() or digest(target) != old.get("sha256"):
                        result["conflicts"].append({**entry, "reason": "本地文件已修改或不受清单管理"})
                        continue
                    if all(old.get("remote", {}).get(k) == item.get(k) for k in ("id", "size", "remote_id")):
                        result["skipped"].append(entry)
                        continue
                elif exists and not overwrite:
                    result["skipped"].append(entry)
                    continue
                backup = None
                if exists:
                    fd, backup_name = tempfile.mkstemp(prefix=".materials-backup-", dir=directory)
                    os.close(fd)
                    backup = Path(backup_name)
                    backup.unlink()
                    os.link(target, backup, follow_symlinks=False)
                def fetch(session):
                    with staged_output(target, overwrite=bool(overwrite or (sync and exists))) as temporary:
                        with temporary.open("wb") as stream:
                            size = session.download(f"{COURSES}/api/uploads/{fid}/blob", stream, check=self.check)
                        if isinstance(item.get("size"), int) and size != item["size"]:
                            raise RemoteError("下载大小与远端元数据不一致")
                        checksum = digest(temporary)
                        if sync and exists and (target.is_symlink() or not target.is_file() or digest(target) != old.get("sha256")):
                            raise LocalFileError("下载期间本地文件发生变化，已保留")
                    return size, checksum
                published = False
                try:
                    size, checksum = self.read(fetch)
                    published = True
                    record = {"remote": item, "path": filename, "size": size, "sha256": checksum}
                    updated = {**manifest, "files": {**manifest["files"], fid: record}}
                    self._save(manifest_path, updated)
                    manifest = updated
                except Exception:
                    if published:
                        if backup is not None:
                            os.replace(backup, target)
                        else:
                            target.unlink(missing_ok=True)
                    raise
                finally:
                    if backup is not None:
                        backup.unlink(missing_ok=True)
                result["downloaded"].append(entry)
            except (OSError, LocalFileError, RemoteError, SessionExpired) as exc:
                result["failed"].append({**entry, "error": str(exc)})
        result["ok"] = not result["failed"] and not result["conflicts"]
        return result

    @staticmethod
    def _save(path, manifest):
        temporary = None
        try:
            fd, name = tempfile.mkstemp(prefix=".materials-", suffix=".tmp", dir=path.parent)
            temporary = Path(name)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(manifest, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class CoursesAdapter:
    def __init__(self, session):
        self.session = session

    def json(self, session, path, *, method="GET", body=None):
        return session.json(
            COURSES + path,
            method=method,
            body=body,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

    def paged(self, session, path, key):
        result = []
        for page in range(1, 1001):
            separator = "&" if "?" in path else "?"
            _, data = self.json(
                session, f"{path}{separator}page={page}&page_size=50")
            rows = required_list(data, key)
            result.extend(rows)
            try:
                pages = int(data.get("pages") or 1)
            except (TypeError, ValueError) as exc:
                raise RemoteError("列表接口页数格式异常") from exc
            if not rows or page >= pages:
                return result
        raise RemoteError("分页超过 1000 页，已停止读取")

    def todos(self):
        def fetch(session):
            _, data = self.json(session, "/api/todos?no-intercept=true")
            key = "todo_list" if "todo_list" in data else "todoList"
            todos = [
                {
                    "id": row.get("id"),
                    "course_name": row.get("course_name") or "",
                    "course_code": row.get("course_code") or "",
                    "title": row.get("title") or "",
                    "type": TODO_TYPES.get(row.get("type"), row.get("type") or "待办"),
                    "deadline": shanghai_date(row.get("end_time")),
                    "end_time_utc": row.get("end_time"),
                }
                for row in required_list(data, key)
            ]
            todos.sort(key=lambda row: row["deadline"] or "9999-12-31")
            return {"count": len(todos), "todos": todos}

        return fetch(self.session)

    def courses(self, keyword="", statuses=None):
        statuses = statuses or ["ongoing", "notStarted", "ended"]
        if not set(statuses) <= {"ongoing", "notStarted", "ended"}:
            raise InputError("课程状态无效")
        condition = json.dumps(
            {"status": statuses, "keyword": keyword or ""},
            ensure_ascii=False,
            separators=(",", ":"),
        )

        def fetch(session):
            rows = self.paged(
                session,
                "/api/my-courses?conditions=" + quote(condition, safe=""),
                "courses",
            )
            courses = [
                {
                    "id": row.get("id"),
                    "name": row.get("name") or "",
                    "course_code": row.get("course_code") or "",
                    "academic_year": (row.get("academic_year") or {}).get("name", ""),
                    "semester": (row.get("semester") or {}).get("name", ""),
                    "department": (row.get("department") or {}).get("name", ""),
                    "teachers": "、".join(
                        teacher.get("name") or ""
                        for teacher in row.get("instructors") or []
                    ),
                    "class": (row.get("course_attributes") or {}).get(
                        "teaching_class_name") or "",
                    "study_completeness": row.get("study_completeness"),
                }
                for row in rows
            ]
            return {"count": len(courses), "courses": courses}

        return fetch(self.session)


class HomeworkAdapter(CoursesAdapter):
    def reserve_upload(self, name, size):
        status, reserve = self.json(
            self.session,
            "/api/uploads",
            method="POST",
            body={
                "name": name,
                "size": size,
                "parent_type": "submission_upload",
                "parent_id": 0,
                "is_scorm": False,
                "embed_material_type": "",
            },
        )
        if status != 201 or not reserve.get("id") or not reserve.get("upload_url"):
            raise RemoteError("占位上传失败或返回字段不完整")
        return reserve

    def upload_content(self, reserve, name, content, timeout):
        url = reserve["upload_url"]
        if urlsplit(url).scheme != "https":
            raise RemoteError("上传地址不是 HTTPS")
        boundary = "zju-" + uuid.uuid4().hex
        filename = name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        multipart = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        multipart += content + f"\r\n--{boundary}--\r\n".encode()
        HttpSession(timeout).request(
            url,
            method="PUT",
            data=multipart,
            headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
        )

    def submit(self, activity_id, upload_id, comment):
        return self.json(
            self.session,
            f"/api/course/activities/{activity_id}/submissions",
            method="POST",
            body={
                "uploads": [upload_id],
                "comment": comment,
                "slides": [],
                "is_draft": False,
            },
        )

    def activity(self, activity_id):
        return self.json(self.session, f"/api/activities/{activity_id}")[1]

    def retract(self, submission_id):
        return self.json(
            self.session,
            f"/api/submissions/{submission_id}/retract",
            method="PUT",
            body={},
        )

    def _json(self, *args, **kwargs):
        return self.json(*args, **kwargs)

    def _paged(self, *args, **kwargs):
        return self.paged(*args, **kwargs)

    def _read(self, operation):
        return operation(self.session)

    def _user_id(self, session, course_id):
        _, data = self._json(
            session, f"/api/course/{course_id}/activity-reads-for-user")
        rows = required_list(data, "activity_reads")
        user_id = (
            rows[0].get("created_for_id") or rows[0].get("created_by_id")
            if rows else None
        )
        if not user_id:
            raise RemoteError("无法解析当前用户 ID")
        return user_id

    def _submission(self, session, activity_id, user_id):
        _, data = self._json(
            session,
            f"/api/activities/{activity_id}/students/{user_id}/submission_list",
        )
        rows = required_list(data, "list")
        return rows[0] if rows else None

    def homework(self, course_id, include_submission=True):
        course_id = identifier(course_id)

        def fetch(session):
            activities = self._paged(
                session,
                f"/api/courses/{course_id}/homework-activities",
                "homework_activities",
            )
            warnings = []
            user_id = None
            if include_submission and activities:
                try:
                    user_id = self._user_id(session, course_id)
                except SessionExpired:
                    raise
                except RemoteError:
                    warnings.append("当前用户 ID 无法解析，未获取提交详情")
            homeworks = []
            for activity in activities:
                item = {
                    "activity_id": activity.get("id"),
                    "title": activity.get("title") or "",
                    "type": TODO_TYPES.get(activity.get("type"), activity.get("type") or ""),
                    "deadline": shanghai_date(
                        activity.get("end_time") or activity.get("deadline")),
                    "end_time_utc": activity.get("end_time") or activity.get("deadline"),
                    "is_ended": bool(activity.get("is_closed")),
                    "in_progress": bool(activity.get("is_in_progress")),
                    "submitted": bool(activity.get("submitted")),
                    "allow_retract": bool(
                        (activity.get("data") or {}).get("allow_retract")),
                    "weight_percent": activity.get("score_percentage"),
                    "score": None,
                }
                if user_id:
                    try:
                        submission = self._submission(
                            session, activity["id"], user_id)
                        if submission:
                            final_score = submission.get("final_score")
                            item["score"] = (
                                final_score
                                if final_score is not None else submission.get("score")
                            )
                            item["submission"] = self._submission_summary(submission)
                    except SessionExpired:
                        raise
                    except RemoteError:
                        warnings.append(
                            f"作业 {activity.get('id')} 的提交详情获取失败")
                homeworks.append(item)
            result = {
                "course_id": int(course_id) if course_id.isdigit() else course_id,
                "count": len(homeworks),
                "homeworks": homeworks,
            }
            if warnings:
                result["warnings"] = warnings
            return result

        return self._read(fetch)

    @staticmethod
    def _submission_summary(submission):
        return {
            "submission_id": submission.get("id"),
            "is_draft": submission.get("is_draft"),
            "can_retract": submission.get("can_retract"),
            "submitted_at": submission.get("submitted_at"),
            "files": [upload.get("name") for upload in submission.get("uploads") or []],
            "score": submission.get("score"),
            "final_score": submission.get("final_score"),
            "rubric_score": submission.get("rubric_score"),
            "graded_at": submission.get("score_at"),
            "instructor_comment": submission.get("instructor_comment") or "",
        }


class MaterialsAdapter(CoursesAdapter):
    def list(self, course_id):
        course_id = identifier(course_id)
        activities = []
        for page in range(1, 1001):
            _, data = self.json(
                self.session,
                f"/api/courses/{course_id}/activities?page={page}&page_size=50",
            )
            activities.extend(required_list(data, "activities"))
            try:
                pages = int(data.get("pages", 1))
                if pages < 1:
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise RemoteError("列表接口页数格式异常") from exc
            if page >= pages:
                break
        else:
            raise RemoteError("分页超过 1000 页，已停止读取")
        files = []
        seen = set()
        for activity in activities:
            if activity.get("type") != "material":
                continue
            for upload in required_list(activity, "uploads"):
                try:
                    file_id = identifier(upload.get("id"))
                except InputError as exc:
                    raise RemoteError("附件 ID 格式异常") from exc
                if file_id in seen:
                    continue
                if not isinstance(upload.get("name"), str) or (
                    upload.get("size") is not None
                    and (
                        isinstance(upload["size"], bool)
                        or not isinstance(upload["size"], int)
                        or upload["size"] < 0
                    )
                ):
                    raise RemoteError("附件名称或大小格式异常")
                seen.add(file_id)
                files.append({
                    "id": file_id,
                    "name": upload.get("name") or "",
                    "size": upload.get("size"),
                    "activity_id": activity.get("id"),
                    "activity_title": activity.get("title") or "",
                    "created_at": upload.get("created_at"),
                    "updated_at": upload.get("updated_at"),
                    "activity_created_at": activity.get("created_at"),
                    "activity_updated_at": activity.get("updated_at"),
                    "activity_start_time": activity.get("start_time"),
                    "activity_end_time": activity.get("end_time"),
                    "remote_id": (
                        upload.get("key")
                        or upload.get("file_id")
                        or upload.get("etag")
                    ),
                })
        return {"course_id": course_id, "count": len(files), "files": files}


SUCCESS = {"on_call", "on_call_fine"}


def completed(value):
    return any(value.get(key) in SUCCESS for key in ("status", "status_name"))


class AttendanceAdapter:
    def __init__(self, session):
        self.session = session

    def activities(self):
        _, value = self.session.json(COURSES + "/api/radar/rollcalls")
        return required_list(value, "rollcalls")

    def details(self, activity_id):
        _, value = self.session.json(
            COURSES
            + f"/api/rollcall/{identifier(activity_id)}/student_rollcalls"
        )
        return value

    def code(self, activity_id):
        value = self.details(activity_id)
        code = value.get("number_code")
        if type(code) is int and 1000 <= code <= 9999:
            return str(code)
        return code if isinstance(code, str) and re.fullmatch(r"[0-9]{4}", code) else None

    def confirmed(self, activity_id):
        rows = self.activities()
        return any(
            str(row.get("rollcall_id")) == str(activity_id) and completed(row)
            for row in rows
        )

    def _submit(self, path, payload):
        status, raw, final = self.session.request(
            COURSES + path,
            method="PUT",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            allowed_statuses=(400, 409, 422),
        )
        if "/cas/login" in urlsplit(final).path:
            raise SessionExpired("签到会话已失效，结果需核实")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise RemoteError("签到响应无法解析，结果需核实") from exc
        if not isinstance(value, dict):
            raise RemoteError("签到响应格式异常，结果需核实")
        return {
            "confirmed": status == 200 and completed(value),
            "rejected": bool(value.get("error_code")),
            "wrong_code": value.get("error_code") == "wrong_number_code",
            "out_of_scope": value.get("error_code") == "radar_out_of_scope",
            "distance": value.get(
                "distance",
                (value.get("data") or {}).get(
                    "distance",
                    (value.get("result") or {}).get("distance"),
                ),
            ),
        }

    def number(self, activity_id, code):
        return self._submit(
            f"/api/rollcall/{identifier(activity_id)}/answer_number_rollcall",
            {"deviceId": str(uuid.uuid4()), "numberCode": code},
        )

    def radar(self, activity_id, point):
        return self._submit(
            f"/api/rollcall/{identifier(activity_id)}/answer?api_version=1.1.2",
            {
                "deviceId": str(uuid.uuid4()),
                "longitude": point[0],
                "latitude": point[1],
                "speed": None,
                "accuracy": 68,
                "altitude": None,
                "altitudeAccuracy": None,
                "heading": None,
            },
        )
