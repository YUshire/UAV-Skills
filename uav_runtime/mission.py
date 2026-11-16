"""Compose existing planning stages into a card and reproducible route evidence."""
from copy import deepcopy

from .atomic.allocation import compute as allocate
from .atomic.common import AtomicError, require
from .atomic.query import compute as query
from .seven_geometry import costs, build
from .seven_inputs import prepare_candidates, resolve_jobs
from .search_targets import validate_targets


def constraints_for(base, supplied):
    require(supplied, "constraints", dict)
    result = deepcopy(supplied)
    fixed = result.setdefault("fixed_assignments", {})
    require(fixed, "fixed_assignments", dict)
    for job, uid in base.get("fixed_assignments", {}).items():
        if job in fixed and fixed[job] != uid:
            raise AtomicError("INVALID_ARGUMENT", "作业指定机与固定归属约束冲突", job_id=job)
        fixed[job] = uid
    single = result.setdefault("single_owner_jobs", [])
    require(single, "single_owner_jobs", list)
    for job in base.get("single_owner_jobs", []):
        if job not in single:
            single.append(job)
    return result


def card_for(plan):
    names = {job["job_id"]: job["name"] for job in plan["jobs"]}
    lines = [f"【规划卡】{plan['task_key']}", "",
             "任务类型为" + ("沿边巡逻" if plan["task_type"] == "patrol" else "区域覆盖搜索") + "，航迹来源为 mock。"]
    if plan.get("targets"):
        lines.append("搜索目标为 " + "、".join(target["description"] for target in plan["targets"]) + "。")
    for job in plan["jobs"]:
        size = (f"圆形，半径 {job['radius']:g} m" if job["shape"] == "circle"
                else f"矩形，{job['length']:g} × {job['width']:g} m")
        lines.append(f"- {job['name']}，{size}，区域来源为 " +
                     ("地点库登记" if job.get("area_source") == "registry" else "用户参数") + "。")
    lines.extend(["", "| 无人机 | 作业顺序 | 航高 m | 预计航程 m | 预计用时 s | 预计剩余电量 |",
                  "| --- | --- | ---: | ---: | ---: | ---: |"])
    for row in plan["assignments"]:
        order = " → ".join(names[job] for job in row["jobs"]) + " → 返航"
        lines.append(f"| {row['uav_id']} | {order} | {row['flight_alt']:g} | {row['estimated_range_m']:.3f} | "
                     f"{row['estimated_time_s']:.3f} | {row['estimated_battery_after']:.2f}% |")
    for row in plan["assignments"]:
        lines.extend(["", f"{row['uav_id']} 的状态记录为电量 {row['battery_level']:g}%，剩余航程 {row['flight_remain']:g} m。"
                      f"预留 20% 电量后的可用预算为 {row['range_budget']:.3f} m、{row['time_budget']:.3f} s。"
                      f"航点共 {sum(len(s.get('waypoints', [])) for s in row['steps'])} 个，"
                      "起点与返航点采用状态库登记坐标。"])
    if plan["task_type"] == "patrol":
        lines.append("\n巡逻圈数为 " + str(plan["laps"]) + "。")
    lines.extend(["", "待命机为 " + ("、".join(plan["unused"]) or "无") + "。",
                  "估算覆盖水平转场和区域航线，起降和悬停耗时另计。mock 航迹用于测试，现场障碍与实际覆盖效果尚待验证。"])
    if not plan["solver"].get("optimal"):
        lines.append("分配已找到可行解，最优性尚未证明。")
    return "\n".join(lines)


def compute(request):
    """Read workspace/data and calculate a complete mock mission planning card."""
    require(request, "input", dict)
    allowed = {"jobs", "task_type", "flight_alt", "laps", "speed", "search_timeout",
               "uav_ids", "exclude_uavs", "payload", "constraints", "solver_options", "targets"}
    if set(request) - allowed:
        raise AtomicError("INVALID_ARGUMENT", "任务含不支持的字段", fields=sorted(set(request) - allowed))
    targets = validate_targets(request["targets"]) if "targets" in request else []
    candidate_args = {k: request[k] for k in ("uav_ids", "exclude_uavs", "payload", "flight_alt") if k in request}
    candidates = prepare_candidates(candidate_args)
    places = query({"entity": "places"})["places"]
    regions = resolve_jobs({"jobs": request.get("jobs"), "places": places})
    parameters = {k: request[k] for k in ("task_type", "flight_alt", "laps", "speed") if k in request}
    estimate = costs(dict(parameters, candidates=candidates["candidates"], jobs=regions["jobs"]))
    allocation_args = {k: estimate[k] for k in ("candidates", "jobs", "costs")}
    allocation_args["constraints"] = constraints_for(estimate["constraints"], request.get("constraints", {}))
    if "solver_options" in request:
        allocation_args["solver_options"] = request["solver_options"]
    allocation = allocate(allocation_args)
    plan = build({"cost_estimate": estimate, "allocation": allocation,
                  "search_timeout": request.get("search_timeout", 1200)})
    plan.update(mock=True, map_context={"backend": "mock", "mock": True},
                targets=targets, excluded=candidates["excluded"],
                cost_estimate=estimate, allocation=allocation)
    plan["card"] = card_for(plan)
    plan["_summary"].update(card=plan["card"], mock=True)
    return plan
