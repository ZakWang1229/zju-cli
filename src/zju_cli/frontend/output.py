from __future__ import annotations

import json
import sys

TABLES = {
    "todos": [("id", "ID"), ("deadline", "截止日期"), ("course_name", "课程"), ("title", "待办")],
    "homeworks": [("activity_id", "ID"), ("deadline", "截止日期"), ("submitted", "已交"), ("score", "得分"), ("title", "作业")],
}


def emit(value: dict, *, as_json=False) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    list_key = next((key for key in ("todos", "homeworks", "courses") if isinstance(value.get(key), list)), None)
    if list_key:
        rows = value[list_key]
        if list_key == "courses":
            columns = ([("sub_id", "节次 ID"), ("start_at", "开始时间"), ("course_title", "课程"), ("status_label", "状态")]
                       if "range" in value else [("id", "ID"), ("name", "课程"), ("teachers", "教师"), ("semester", "学期")])
        else:
            columns = TABLES[list_key]
        if "range" in value:
            print(f"日期：{value['range']}")
        print(f"共 {len(rows)} 条")
        print("\t".join(label for _, label in columns))
        for row in rows:
            print("\t".join("-" if row.get(key) is None else str(row[key]).replace("\n", " ").replace("\t", " ") for key, _ in columns))
    else:
        for key, item in value.items():
            if key != "warnings":
                print(f"{key}: {item}")
    for warning in value.get("warnings", []):
        log(f"提示：{warning}")


def log(message: str) -> None:
    print(message, file=sys.stderr)
