"""CLI business facade; no school HTTP requests execute in the CLI."""
from pathlib import Path
from zju_cli.frontend.rpc import DaemonClient


class RemoteService:
    def __init__(self, settings, **kwargs):
        self.settings = settings
        self.client = DaemonClient(settings)

    def _call(self, operation, **parameters):
        names = {"todos": "courses.todos", "courses": "courses.list",
            "homework": "courses.homework.list", "submit_homework": "courses.homework.submit",
            "retract_homework": "courses.homework.retract", "schedule": "classroom.schedule"}
        return self.client.execute(names.get(operation, operation), parameters)

    def todos(self):
        return self._call("todos")

    def courses(self, keyword="", statuses=None):
        return self._call("courses", keyword=keyword, statuses=statuses)

    def homework(self, course_id, include_submission=True):
        return self._call("homework", course_id=course_id, include_submission=include_submission)

    def submit_homework(self, activity_id, file_path, comment=""):
        return self._call("submit_homework", activity_id=activity_id,
                          file_path=str(Path(file_path).expanduser().resolve()), comment=comment)

    def retract_homework(self, activity_id):
        return self._call("retract_homework", activity_id=activity_id)

    def schedule(self, start_date=None, end_date=None):
        return self._call("schedule", start_date=start_date, end_date=end_date)
