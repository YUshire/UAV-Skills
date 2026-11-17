"""Separate capability checks for multi-step tasks.

A step is one action at one place.  The same place may appear twice with
different action types.  Keep the three questions independent: action-type
mix, parameter differences, and required order.
"""
from .atomic.common import AtomicError, require, required


_TYPES = ("search", "patrol")
# Accept both compact checker steps and fully resolved jobs; only the fields
# below affect capability, the rest are carried through from the task model.
_STEP_FIELDS = {"job_id", "name", "place", "center", "center_lon", "center_lat",
                "shape", "radius", "length", "width", "entrance", "fixed_uav",
                "area_source", "task_type", "laps", "search_timeout", "uav_id",
                "work_speed"}


def check_task(request):
    """Check mixed types, mixed parameters and explicit order without planning."""
    require(request, "input", dict)
    if set(request) - {"steps", "ordered"}:
        raise AtomicError("INVALID_ARGUMENT", "capability 输入含不支持的字段")
    steps = require(required(request, "steps"), "steps", list)
    if not 1 <= len(steps) <= 64:
        raise AtomicError("INVALID_ARGUMENT", "steps 数量须为1..64")
    ordered = request.get("ordered", False)
    if type(ordered) is not bool:
        raise AtomicError("INVALID_ARGUMENT", "ordered 须为布尔值")

    gaps, seen = [], set()
    types, laps_values, timeout_values, fixed_uavs = [], set(), set(), set()
    for index, step in enumerate(steps):
        path = "/steps/" + str(index)
        require(step, "steps[]", dict)
        if set(step) - _STEP_FIELDS:
            raise AtomicError("INVALID_ARGUMENT", "步骤含不支持的字段",
                              fields=sorted(set(step) - _STEP_FIELDS))
        jid = step.get("job_id", "J" + str(index + 1))
        if not isinstance(jid, str) or not jid.strip() or jid in seen:
            gaps.append({"path": path + "/job_id", "code": "INVALID_STEP_ID",
                         "message": "步骤编号须唯一且非空"})
            continue
        seen.add(jid)
        task_type = step.get("task_type", "search")
        if task_type not in _TYPES:
            gaps.append({"path": path + "/task_type", "code": "UNSUPPORTED_ACTION",
                         "message": "只支持 search 或 patrol"})
            continue
        types.append(task_type)
        if "laps" in step:
            laps = step["laps"]
            if type(laps) is not int or laps < 1 or (task_type != "patrol" and laps != 1):
                gaps.append({"path": path + "/laps", "code": "INVALID_PARAMETER",
                             "message": "laps 须为正整数且只用于巡逻"})
            elif task_type == "patrol":
                laps_values.add(laps)
        if "search_timeout" in step:
            timeout = step["search_timeout"]
            if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                    or timeout <= 0 or task_type != "search"):
                gaps.append({"path": path + "/search_timeout", "code": "INVALID_PARAMETER",
                             "message": "search_timeout 须为正数且只用于搜索"})
            else:
                timeout_values.add(timeout)
        if "uav_id" in step:
            uid = step["uav_id"]
            if not isinstance(uid, str) or not uid.strip():
                gaps.append({"path": path + "/uav_id", "code": "INVALID_PARAMETER",
                             "message": "uav_id 须为非空字符串"})
            else:
                fixed_uavs.add(uid)

    mixed_types = len(set(types)) > 1
    mixed_parameters = len(laps_values) > 1 or len(timeout_values) > 1
    order_scope = "per_aircraft"
    if ordered and len(fixed_uavs) > 1:
        gaps.append({"path": "/ordered", "code": "CROSS_UAV_ORDER",
                     "message": "固定到不同无人机的步骤之间不保证先后顺序"})
        order_scope = "cross_aircraft"
    checks = {
        "mixed_action_types": {"supported": True, "mixed": mixed_types,
                               "types": sorted(set(types))},
        "mixed_parameters": {"supported": True, "mixed": mixed_parameters,
                             "fields": {key: sorted(values) for key, values in
                                        (("laps", laps_values), ("search_timeout", timeout_values))
                                        if values}},
        "explicit_order": {"supported": not (ordered and len(fixed_uavs) > 1),
                           "ordered": ordered, "scope": order_scope},
    }
    route = "full" if not gaps else "preview"
    return {"status": "OK", "kind": "task_capability", "route": route,
            "step_count": len(steps), "gaps": gaps, "checks": checks,
            "submitted": False,
            "_summary": {"route": route, "step_count": len(steps),
                         "gap_count": len(gaps), "checks": checks}}
