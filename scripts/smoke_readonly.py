"""Optional live smoke check; never submits or retracts homework."""

import argparse

from zju_cli.shared.config import Settings
from zju_cli.frontend.output import log
from zju_cli.frontend.remote_service import RemoteService as ZjuService


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date")
    parser.add_argument("--end-date")
    args = parser.parse_args()
    service = ZjuService(Settings.load(), on_progress=log)
    print("todos:", service.todos()["count"])
    courses = service.courses(statuses=["ongoing"])
    print("courses:", courses["count"])
    if courses["courses"]:
        course = next((row for row in courses["courses"] if "数学分析" in row["name"]), courses["courses"][0])
        homeworks = service.homework(course["id"])
        print("homeworks:", homeworks["count"], "warnings:", len(homeworks.get("warnings", [])))
    schedule = service.schedule(args.date, args.end_date)
    print("schedule:", schedule["count"])


if __name__ == "__main__":
    main()
