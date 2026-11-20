#!/usr/bin/env python3
"""从本次 build 产物组装原始动作、离线校验，并保存完整序列及报告。"""
from copy import deepcopy
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from uav_runtime.atomic.common import AtomicError, require, required, run
import submit_queue_batch as batch_validator


REPORT_FIELDS = ("uav_id", "flight_alt", "jobs", "battery_level", "flight_remain",
                 "range_budget", "time_budget", "estimated_range_m", "estimated_battery_after")
SHOW_ACTIONS = os.environ.get("UAV_SHOW_ACTIONS", "compact").strip().lower()


def _actions_view(raw):
    """Show the assembled sequence without repeating long waypoint arrays."""
    view = []
    for action in raw:
        params = deepcopy(action.get("params", {}))
        waypoints = params.get("waypoints")
        if isinstance(waypoints, list):
            params["waypoints"] = {"count": len(waypoints),
                                   "first": waypoints[0] if waypoints else None,
                                   "last": waypoints[-1] if waypoints else None}
        view.append({"cmd": action["cmd"], "task_id": action["task_id"],
                     "step_id": action["step_id"], "params": params})
    return view


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
    diagnostics, failures = [], []
    max_distance = batch_validator.DEFAULT_MAX_DISTANCE_M
    for assignment in assignments:
        require(assignment, "assignment", dict)
        uid = required(assignment, "uav_id")
        if not isinstance(uid, str) or not uid.strip() or uid in seen:
            raise AtomicError("INVALID_ARGUMENT", "uav_id 须为唯一非空字符串")
        seen.add(uid)
        task_id = f"{task_key}-{uid}"
        raw = []
        speed = assignment.get("speed") if assignment.get("speed") is not None else plan.get("speed")

        def add(cmd, params):
            params = deepcopy(params)
            if speed is not None and cmd in ("takeoff", "follow_path", "patrol_move", "fuel_explore"):
                params["max_vel" if cmd == "fuel_explore" else "speed"] = speed
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
            batch = batch_validator.validate_batch(raw, origin_lat=required(origin, "latitude"),
                                                   origin_lon=required(origin, "longitude"),
                                                   max_distance_m=max_distance)
        except batch_validator.ValidationError as exc:
            failures.append({"uav_id": uid, "message": str(exc)})
            diagnostics.append({"uav_id": uid, "status": "INVALID_ARGUMENT",
                                "message": str(exc), "validated": False, "submitted": False})
            continue
        report = {key: deepcopy(required(assignment, key)) for key in REPORT_FIELDS}
        report.update(action_count=len(batch), validated=True, submitted=False)
        if SHOW_ACTIONS not in ("0", "false", "off", "no"):
            report["actions"] = deepcopy(raw) if SHOW_ACTIONS == "full" else _actions_view(raw)
            if SHOW_ACTIONS == "full":
                report["local_batch"] = deepcopy(batch)
        participants.append({"uav_id": uid, "task_id": task_id, "origin": deepcopy(origin),
                             "raw_batch": raw, "dry_run": {"status": "OK", "validated": True,
                             "submitted": False, "action_count": len(batch), "batch": batch}, "report": report})
        diagnostics.append({"uav_id": uid, "status": "OK", "validated": True,
                            "submitted": False, "action_count": len(batch)})
    if failures:
        raise AtomicError("INVALID_ARGUMENT", failures[0]["message"], uav_id=failures[0]["uav_id"],
                          max_distance_m=max_distance, validated=False, submitted=False,
                          validated_for_execution=False, participants=diagnostics)
    output = {"status": "OK", "task_key": task_key, "validated": True, "submitted": False,
              "stage": "VALIDATED", "execution_state": "NOT_STARTED", "validated_for_execution": False,
              "validation_scope": "action_format_and_coordinate_conversion", "participants": participants,
              "max_distance_m": max_distance}
    for key in ("mock", "map_used", "whole_mission_budget_complete", "budget_scope", "unused", "jobs"):
        output[key] = deepcopy(required(plan, key))
    output["_summary"] = {key: deepcopy(output[key]) for key in
                          ("task_key", "validated", "submitted", "validated_for_execution", "mock", "map_used",
                           "whole_mission_budget_complete", "budget_scope", "max_distance_m")}
    output["_summary"]["participants"] = [p["report"] for p in participants]
    output["_summary"]["show_actions"] = SHOW_ACTIONS
    return output


if __name__ == "__main__":
    raise SystemExit(run(compute, "flight_actions"))
