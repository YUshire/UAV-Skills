#!/usr/bin/env python3
"""从本次 build 产物组装原始动作、离线校验，并保存完整序列及报告。"""
from copy import deepcopy
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from uav_runtime.atomic.common import AtomicError, require, required, run
from submit_queue_batch import ValidationError, validate_batch


REPORT_FIELDS = ("uav_id", "flight_alt", "jobs", "battery_level", "flight_remain",
                 "range_budget", "time_budget", "estimated_range_m", "estimated_battery_after")


def compute(request):
    """输入 plan（可 ref/select 引用 build 的 /data）；只组装和 dry-run，不下发。"""
    require(request, "input", dict)
    if set(request) != {"plan"}:
        raise AtomicError("INVALID_ARGUMENT", "仅接收 plan 字段")
    plan = require(required(request, "plan"), "plan", dict)
    if plan.get("status") != "OK":
        raise AtomicError("INVALID_ARGUMENT", "plan 须为成功的 build 结果")
    task_key = required(plan, "task_key")
    if not isinstance(task_key, str) or not task_key.strip():
        raise AtomicError("INVALID_ARGUMENT", "task_key 须为非空字符串")
    assignments = require(required(plan, "assignments"), "assignments", list)
    if not assignments:
        raise AtomicError("INVALID_ARGUMENT", "assignments 不能为空")
    participants, seen = [], set()
    for assignment in assignments:
        require(assignment, "assignment", dict)
        uid = required(assignment, "uav_id")
        if not isinstance(uid, str) or not uid.strip() or uid in seen:
            raise AtomicError("INVALID_ARGUMENT", "uav_id 须为唯一非空字符串")
        seen.add(uid)
        task_id = f"{task_key}-{uid}"
        raw = []

        def add(cmd, params):
            params = deepcopy(params)
            if plan.get("speed") is not None and cmd in ("takeoff", "follow_path", "patrol_move", "fuel_explore"):
                params["max_vel" if cmd == "fuel_explore" else "speed"] = plan["speed"]
            raw.append({"cmd": cmd, "task_id": task_id, "step_id": len(raw) + 1, "params": params})

        add("arm", {})
        add("takeoff", {"altitude": required(assignment, "flight_alt")})
        for step in require(required(assignment, "steps"), "steps", list):
            require(step, "step", dict)
            kind = required(step, "type")
            if kind == "transit":
                add("follow_path", {"waypoints": required(step, "waypoints"), "skip_unreachable": False})
            elif kind == "search":
                add("fuel_explore", {key: required(step, key) for key in ("box_min", "box_max", "timeout_sec")})
            elif kind == "patrol":
                laps = required(step, "laps")
                if type(laps) is not int or laps < 1:
                    raise AtomicError("INVALID_ARGUMENT", "巡逻 laps 须为正整数")
                for _ in range(laps):
                    add("patrol_move", {"waypoints": required(step, "waypoints"), "skip_unreachable": False})
            else:
                raise AtomicError("INVALID_ARGUMENT", "不支持的航段类型", step_type=kind)
        add("land", {})
        origin = require(required(assignment, "origin"), "origin", dict)
        try:
            batch = validate_batch(raw, origin_lat=required(origin, "latitude"),
                                   origin_lon=required(origin, "longitude"))
        except ValidationError as exc:
            raise AtomicError("INVALID_ARGUMENT", str(exc), uav_id=uid) from exc
        report = {key: deepcopy(required(assignment, key)) for key in REPORT_FIELDS}
        report.update(action_count=len(batch), validated=True, submitted=False)
        participants.append({"uav_id": uid, "task_id": task_id, "origin": deepcopy(origin),
                             "raw_batch": raw, "dry_run": {"status": "OK", "validated": True,
                             "submitted": False, "action_count": len(batch), "batch": batch}, "report": report})
    output = {"status": "OK", "task_key": task_key, "validated": True, "submitted": False,
              "stage": "VALIDATED", "execution_state": "NOT_STARTED", "validated_for_execution": False,
              "validation_scope": "action_format_and_coordinate_conversion", "participants": participants}
    for key in ("mock", "map_used", "whole_mission_budget_complete", "budget_scope", "unused", "jobs"):
        output[key] = deepcopy(required(plan, key))
    output["_summary"] = {key: deepcopy(output[key]) for key in
                          ("task_key", "validated", "submitted", "validated_for_execution", "mock", "map_used",
                           "whole_mission_budget_complete", "budget_scope")}
    output["_summary"]["participants"] = [p["report"] for p in participants]
    return output


if __name__ == "__main__":
    raise SystemExit(run(compute, "flight_actions"))
