"""Planning costs and final routes for the original seven-skill workspace.

This module consumes explicit records and solver decisions. It never queries a
database, selects an aircraft, invokes a solver, or publishes flight commands.
"""
from copy import deepcopy
from decimal import Decimal
import math
import time
import uuid

from uav_runtime.atomic.common import AtomicError, digest, finite, require, required
from uav_runtime.direct_path import direct_route
from uav_runtime.planning_geometry import (GEOMETRY_MODEL,
                                          build_steps, cost_geometry)
from uav_runtime.planning.allocation_contract import AllocationEvidenceError, verify_allocation
from uav_runtime.planning.scheduler import validate_scenario, verify_result
from uav_runtime.zone_allocation import partition_rectangle


def _same(actual, expected, message):
    if actual != expected:
        raise AtomicError("ALLOCATION_EVIDENCE_MISMATCH", message)


def _records(rows, key, label, limit=64):
    require(rows, label, list)
    if not 1 <= len(rows) <= limit:
        raise AtomicError("INVALID_ARGUMENT", label + f" 数量须为 1..{limit}")
    names = []
    for row in rows:
        require(row, label + "[]", dict)
        value = required(row, key)
        if not isinstance(value, str) or not value.strip() or value in names:
            raise AtomicError("INVALID_ARGUMENT", label + " 编号须唯一且非空")
        names.append(value)
    return names


def _prepared(request, candidate_limit=64):
    candidates = deepcopy(required(request, "candidates"))
    jobs = deepcopy(required(request, "jobs"))
    _records(candidates, "uav_id", "candidates", candidate_limit)
    _records(jobs, "job_id", "jobs")
    task_type = request.get("task_type", "search")
    if task_type not in ("search", "patrol"):
        raise AtomicError("INVALID_ARGUMENT", "task_type 须为 search 或 patrol")
    altitude = finite(request.get("flight_alt", 1.2), "flight_alt", True)
    laps = request.get("laps", 1)
    if type(laps) is not int or laps < 1 or (task_type != "patrol" and laps != 1):
        raise AtomicError("INVALID_ARGUMENT", "laps 须为正整数且只用于巡逻")
    speed = request.get("speed")
    if speed is not None:
        speed = finite(speed, "speed", True)
    for candidate in candidates:
        uid = candidate["uav_id"]
        for key in ("range_budget", "time_budget", "flight_remain", "battery", "max_altitude"):
            finite(required(candidate, key), uid + "." + key, True)
        origin = require(required(candidate, "origin"), uid + ".origin", dict)
        lon = finite(required(origin, "longitude"), uid + ".longitude")
        lat = finite(required(origin, "latitude"), uid + ".latitude")
        if not -180 <= lon <= 180 or not -90 <= lat <= 90:
            raise AtomicError("INVALID_ARGUMENT", "候选机原点超出经纬度范围")
        if altitude > candidate["max_altitude"]:
            raise AtomicError("ALTITUDE_INFEASIBLE", "基准航高超过候选机上限", uav_id=uid)
        # Speed is a screening condition, not a fatal input error: a global
        # speed must narrow the candidate pool rather than abort the whole run.
        finite(required(candidate, "max_speed"), uid + ".max_speed", True)
    for job in jobs:
        center = require(required(job, "center"), "jobs.center", list)
        if len(center) != 2:
            raise AtomicError("INVALID_ARGUMENT", "jobs.center 须为[经度,纬度]")
        lon, lat = [finite(v, "jobs.center") for v in center]
        if not -180 <= lon <= 180 or not -90 <= lat <= 90:
            raise AtomicError("INVALID_ARGUMENT", "作业中心超出经纬度范围")
        if job.get("shape") == "rectangle":
            finite(required(job, "length"), "length", True)
            finite(required(job, "width"), "width", True)
        elif job.get("shape") == "circle":
            finite(required(job, "radius"), "radius", True)
        else:
            raise AtomicError("INVALID_ARGUMENT", "区域须为rectangle或circle")
    return {"candidates": candidates, "jobs": jobs, "task_type": task_type,
            "flight_alt": altitude, "laps": laps,
            "speeds": {"transit": speed or 1.2, "work": speed or (1.2 if task_type == "patrol" else 0.6)},
            "requested_speed": speed}


_SCREENING_NEAREST_LIMIT = 5
_SCREENING_SAMPLE_LIMIT = 20


def _exclusion_reason(bound):
    """Name the bound(s) that actually failed for one candidate/job pair."""
    if not bound["range_ok"] and not bound["time_ok"]:
        return "BOTH_LOWER_BOUNDS", ["range", "time"]
    if not bound["range_ok"]:
        return "RANGE_LOWER_BOUND", ["range"]
    return "TIME_LOWER_BOUND", ["time"]


def _nearest_entry(candidate, item):
    """Compact per-aircraft evidence for the closest excluded candidates."""
    bound = item["best_bound"]
    entry = {"uav_id": item["uav_id"], "job_id": bound["job_id"],
             "distance_m": bound["distance_m"],
             "round_trip_lower_bound_m": bound["round_trip_lower_bound_m"],
             "range_budget": item["range_budget"], "range_ok": bound["range_ok"],
             "time_lower_bound_s": bound["time_lower_bound_s"],
             "time_budget": item["time_budget"], "time_ok": bound["time_ok"],
             "reason": item["reason"],
             "speed_ok": item.get("speed_ok", True),
             "max_speed_mps": candidate.get("max_speed"),
             "requested_transit_speed_mps": item.get("requested_transit_speed_mps"),
             "requested_work_speed_mps": item.get("requested_work_speed_mps")}
    cruise = candidate.get("cruise_speed")
    if isinstance(cruise, (int, float)) and not isinstance(cruise, bool) and cruise > 0:
        cruise_time = bound["round_trip_lower_bound_m"] / cruise
        entry["cruise_speed_mps"] = cruise
        entry["time_lower_bound_at_cruise_s"] = cruise_time
        entry["time_ok_at_cruise"] = cruise_time <= item["time_budget"] + 1e-6
    return entry


def _zero_retained_diagnosis(nearest, fastest):
    """Explain the single most promising excluded candidate in actionable terms."""
    if not nearest:
        return {"binding_constraint": "NO_CANDIDATE",
                "actionable": "没有可用于比对的候选机。"}
    top = nearest[0]
    if not top.get("speed_ok", True):
        binding = "SPEED_LIMIT"
        actionable = (f"最近候选 {top['uav_id']} 最大速度 {top['max_speed_mps']:g} m/s 低于规划速度"
                      f"（转场 {top['requested_transit_speed_mps']:g} / 作业 "
                      f"{top['requested_work_speed_mps']:g} m/s）；可降低速度，或改用支持该速度的机型。")
    elif top["range_ok"] and not top["time_ok"]:
        binding = "TIME_LOWER_BOUND"
        if top.get("time_ok_at_cruise") and top.get("cruise_speed_mps"):
            actionable = (f"最近候选 {top['uav_id']} 距目标中心 {top['distance_m']:.0f} m，"
                          f"往返航程下界 {top['round_trip_lower_bound_m']:.0f} m 未超过航程预算 "
                          f"{top['range_budget']:.0f} m；但按默认转场速度 {fastest:g} m/s 往返需 "
                          f"{top['time_lower_bound_s']:.0f} s，超过时间预算 {top['time_budget']:.0f} s。"
                          f"按其巡航速度 {top['cruise_speed_mps']:g} m/s 约需 "
                          f"{top['time_lower_bound_at_cruise_s']:.0f} s。可指定不超过机型 max_speed 的"
                          f"速度后重算（指定全局速度时需把候选范围缩小到支持该速度的机型），"
                          f"不能笼统判为航程不足。")
        else:
            actionable = (f"最近候选 {top['uav_id']} 距目标中心 {top['distance_m']:.0f} m，"
                          f"往返航程下界 {top['round_trip_lower_bound_m']:.0f} m 未超过航程预算 "
                          f"{top['range_budget']:.0f} m；但按默认转场速度 {fastest:g} m/s 往返需 "
                          f"{top['time_lower_bound_s']:.0f} s，超过时间预算 {top['time_budget']:.0f} s。"
                          f"可指定不超过机型 max_speed 的速度后重算（指定全局速度时需把候选范围缩小到"
                          f"支持该速度的机型），不能笼统判为航程不足。")
    elif not top["range_ok"] and top["time_ok"]:
        binding = "RANGE_LOWER_BOUND"
        actionable = (f"最近候选 {top['uav_id']} 距目标中心 {top['distance_m']:.0f} m，"
                      f"往返航程下界 {top['round_trip_lower_bound_m']:.0f} m 超过航程预算 "
                      f"{top['range_budget']:.0f} m。")
    else:
        binding = "BOTH_LOWER_BOUNDS"
        actionable = (f"最近候选 {top['uav_id']} 的往返航程下界 {top['round_trip_lower_bound_m']:.0f} m "
                      f"和时间下界 {top['time_lower_bound_s']:.0f} s 分别超过预算 "
                      f"{top['range_budget']:.0f} m / {top['time_budget']:.0f} s。")
    return {"binding_constraint": binding, "nearest_uav_id": top["uav_id"],
            "nearest_job_id": top["job_id"], "actionable": actionable}


def _actual_reach_diagnosis(prepared, job_ids):
    """Explain why no retained candidate can reach any job on actual transit."""
    speeds = prepared["speeds"]
    rows = []
    for candidate in prepared["candidates"]:
        uid = candidate["uav_id"]
        origin = candidate["origin"]
        for job_id, job in zip(job_ids, prepared["jobs"]):
            distance = direct_route((origin["longitude"], origin["latitude"]),
                                    job["center"], prepared["flight_alt"], points=0)["one_way_distance"]
            round_range = 2 * distance
            round_time = round_range / speeds["transit"]
            rows.append((round_range, round_time, uid, job_id, candidate))
    if not rows:
        return {"binding_constraint": "NO_CANDIDATE",
                "actionable": "没有可用于比对的候选机。"}
    round_range, round_time, uid, job_id, candidate = min(
        rows, key=lambda row: (row[0], row[1]))
    range_ok = round_range <= candidate["range_budget"] + 1e-6
    time_ok = round_time <= candidate["time_budget"] + 1e-6
    failed = [name for name, ok in (("range", range_ok), ("time", time_ok)) if not ok]
    if len(failed) == 2:
        binding = "BOTH_LOWER_BOUNDS"
    elif failed == ["range"]:
        binding = "RANGE_LOWER_BOUND"
    else:
        binding = "TIME_LOWER_BOUND"
    actionable = (f"最近候选 {uid} 按实际转场往返 {round_range:.0f} m / {round_time:.0f} s "
                  f"仍超过预算 {candidate['range_budget']:.0f} m / "
                  f"{candidate['time_budget']:.0f} s。")
    return {"binding_constraint": binding, "nearest_uav_id": uid,
            "nearest_job_id": job_id, "actionable": actionable}


def _screen_candidates(prepared):
    """Prune only aircraft unable to reach any job area and return.

    A circumscribed disk encloses every possible rectangle partition. Omitting
    work cost keeps this a necessary bound even when a job is shared. Use the
    fastest planned horizontal speed for a conservative time lower bound.

    The returned screening block distinguishes the range bound from the time
    bound, keeps a compact closest-candidate diagnosis, and returns only a
    capped per-aircraft sample so the tool result stays small enough to be read
    without truncation.
    """
    retained, excluded, fixed_infeasible = [], [], []
    speeds = prepared["speeds"]
    fastest = max(speeds.values())
    for candidate in prepared["candidates"]:
        origin = candidate["origin"]
        speed_ok = all(s <= candidate["max_speed"] + 1e-9 for s in speeds.values())
        bounds = []
        for job in prepared["jobs"]:
            radius = (job["radius"] if job["shape"] == "circle"
                      else math.hypot(job["length"], job["width"]) / 2)
            distance = direct_route((origin["longitude"], origin["latitude"]),
                                    job["center"], prepared["flight_alt"], points=0)["one_way_distance"]
            # direct_route rounds to millimetres; bias downward to preserve a lower bound.
            lower = 2 * max(0.0, distance - radius - 0.001)
            bound = {"job_id": job["job_id"], "distance_m": distance,
                     "round_trip_lower_bound_m": lower,
                     "time_lower_bound_s": lower / fastest,
                     "range_ok": lower <= candidate["range_budget"] + 1e-6,
                     "time_ok": lower / fastest <= candidate["time_budget"] + 1e-6}
            bounds.append(bound)
            if job.get("fixed_uav") == candidate["uav_id"] and not (
                    speed_ok and bound["range_ok"] and bound["time_ok"]):
                fixed_infeasible.append(dict(bound, uav_id=candidate["uav_id"],
                                             range_budget=candidate["range_budget"],
                                             time_budget=candidate["time_budget"],
                                             speed_ok=speed_ok,
                                             max_speed_mps=candidate["max_speed"],
                                             requested_transit_speed_mps=speeds["transit"],
                                             requested_work_speed_mps=speeds["work"]))
        if speed_ok and any(b["range_ok"] and b["time_ok"] for b in bounds):
            retained.append(candidate)
            continue
        best = min(bounds, key=lambda b: (b["round_trip_lower_bound_m"], b["time_lower_bound_s"]))
        if not speed_ok:
            reason = "SPEED_LIMIT"
            failed = ["speed"] + [name for name in ("range", "time") if not best[name + "_ok"]]
        else:
            reason, failed = _exclusion_reason(best)
        excluded.append({"uav_id": candidate["uav_id"], "reason": reason,
                         "failed_bounds": failed,
                         "range_budget": candidate["range_budget"],
                         "time_budget": candidate["time_budget"],
                         "speed_ok": speed_ok,
                         "max_speed_mps": candidate["max_speed"],
                         "requested_transit_speed_mps": speeds["transit"],
                         "requested_work_speed_mps": speeds["work"],
                         "best_bound": best})

    reasons = {"RANGE_LOWER_BOUND": 0, "TIME_LOWER_BOUND": 0, "BOTH_LOWER_BOUNDS": 0,
               "SPEED_LIMIT": 0}
    for item in excluded:
        reasons[item["reason"]] += 1
    ordered = sorted(excluded, key=lambda item: (
        item["best_bound"]["round_trip_lower_bound_m"],
        item["best_bound"]["time_lower_bound_s"]))
    by_id = {c["uav_id"]: c for c in prepared["candidates"]}
    nearest = [_nearest_entry(by_id[item["uav_id"]], item)
               for item in ordered[:_SCREENING_NEAREST_LIMIT]]
    screening = {"method": "area_round_trip_lower_bound",
                 "input_count": len(prepared["candidates"]),
                 "retained_count": len(retained), "excluded_count": len(excluded),
                 "assumed_transit_speed_mps": fastest,
                 "transit_speed_mps": speeds["transit"],
                 "work_speed_mps": speeds["work"],
                 "requested_speed_mps": prepared.get("requested_speed"),
                 "reasons": reasons,
                 "nearest": nearest,
                 "range_ok_but_time_fail": [item["uav_id"] for item in ordered
                                            if item.get("speed_ok", True)
                                            and item["best_bound"]["range_ok"]
                                            and not item["best_bound"]["time_ok"]],
                 "excluded": ordered[:_SCREENING_SAMPLE_LIMIT],
                 "excluded_omitted": max(0, len(excluded) - _SCREENING_SAMPLE_LIMIT)}
    if not retained:
        screening["diagnosis"] = _zero_retained_diagnosis(nearest, fastest)
    retained_ids = {u["uav_id"] for u in retained}
    fixed_missing = {j["job_id"]: j["fixed_uav"] for j in prepared["jobs"]
                     if j.get("fixed_uav") and j["fixed_uav"] not in retained_ids}
    if not retained or fixed_missing or fixed_infeasible:
        diagnosis = screening.get("diagnosis")
        if diagnosis:
            message = "没有可行候选机；" + diagnosis["actionable"]
        else:
            message = "往返航程/时间下界已超限，或固定执行机不在可用候选中"
        raise AtomicError("NO_FEASIBLE_ALLOCATION", message,
                          fixed_assignments_unavailable=fixed_missing,
                          fixed_assignments_infeasible=fixed_infeasible, candidate_screening=screening)
    if len(retained) > 64:
        raise AtomicError("CANDIDATE_LIMIT_EXCEEDED", "预算预筛后仍超过求解器64架上限，需扩展求解规模或提供有依据的任务候选范围",
                          limit=64, candidate_screening=screening)
    prepared["candidates"] = retained
    return screening


def costs(request):
    """Calculate numerical allocation costs from supplied aircraft and regions."""
    if set(request) - {"operation", "candidates", "jobs", "task_type", "flight_alt", "laps", "speed"}:
        raise AtomicError("INVALID_ARGUMENT", "costs 输入含不支持字段")
    prepared = _prepared(request, candidate_limit=4096)
    screening = _screen_candidates(prepared)
    answer = cost_geometry(prepared)
    reachable = answer["costs"]["reachable"]
    kept = [candidate for candidate in prepared["candidates"]
            if reachable.get(candidate["uav_id"])]
    if not kept:
        diagnosis = screening.get("diagnosis") or _actual_reach_diagnosis(
            prepared, answer["job_ids"])
        screening["reachable_count"] = 0
        raise AtomicError("NO_FEASIBLE_ALLOCATION",
                          "按实际往返转场，没有候选机能到达任何作业；" + diagnosis["actionable"],
                          candidate_screening=screening)
    if len(kept) != len(prepared["candidates"]):
        screening["cost_excluded_count"] = len(prepared["candidates"]) - len(kept)
        prepared["candidates"] = kept
        for key in ("transfer_range", "transfer_time", "reachable"):
            answer["costs"][key] = {candidate["uav_id"]: answer["costs"][key][candidate["uav_id"]]
                                    for candidate in kept}
    screening["reachable_count"] = len(kept)
    fixed = {job["job_id"]: job["fixed_uav"] for job in prepared["jobs"] if job.get("fixed_uav")}
    # A circular patrol must stay with one aircraft to preserve the complete
    # boundary. A circular search area is converted to an axis-aligned search
    # box and may be split between two owners, so it must not force single
    # ownership.
    single = [job["job_id"] for job in prepared["jobs"]
              if job["shape"] == "circle" and prepared["task_type"] == "patrol"]
    constraints = {}
    if fixed:
        constraints["fixed_assignments"] = fixed
    if single:
        constraints["single_owner_jobs"] = single
    output = {"status": "OK", "prepared": prepared, "prepared_digest": digest(prepared),
              "candidates": deepcopy(prepared["candidates"]),
              "jobs": [{"job_id": j["job_id"], "name": j.get("name", j["job_id"])} for j in prepared["jobs"]],
              "costs": answer["costs"], "constraints": constraints,
              "candidate_screening": screening, "submitted": False}
    output["_summary"] = {"job_ids": answer["job_ids"], "candidate_ids": [u["uav_id"] for u in prepared["candidates"]],
                          "constraints": constraints, "estimated_only": True,
                          "candidate_screening": {k: v for k, v in screening.items() if k != "excluded"},
                          "note": "成本估计已生成；尚未分配无人机或生成最终航迹"}
    output.update(geometry_model=GEOMETRY_MODEL, map_used=False, mock=True,
                  map_context={"backend": "mock", "mock": True})
    output["_summary"].update(geometry_model=GEOMETRY_MODEL, map_used=False)
    return output


def _validate_allocation(estimate, allocation):
    prepared = require(required(estimate, "prepared"), "prepared", dict)
    _same(digest(prepared), required(estimate, "prepared_digest"), "区域或参数与成本记录不一致")
    _same(estimate["candidates"], prepared["candidates"], "成本候选机与完整记录不一致")
    simple_jobs = [{"job_id": j["job_id"], "name": j.get("name", j["job_id"])} for j in prepared["jobs"]]
    _same(estimate["jobs"], simple_jobs, "成本作业与完整记录不一致")
    uids = _records(prepared["candidates"], "uav_id", "candidates")
    jids = _records(prepared["jobs"], "job_id", "jobs")
    _same(allocation.get("index"), {"uav_ids": uids, "job_ids": jids}, "分配索引与成本记录不一致")
    try:
        check = verify_allocation(required(allocation, "assignments"), allocation)
    except AllocationEvidenceError as exc:
        raise AtomicError(exc.code, str(exc), **exc.details) from exc
    scenario = require(required(allocation, "solver_input"), "solver_input", dict)
    result = required(allocation, "solver_result")
    _same(digest(scenario), result.get("scenario_digest"), "原始求解输入摘要不一致")
    _same((scenario.get("M"), scenario.get("N")), (len(uids), len(jids)), "原始求解数量不一致")
    numeric = require(required(estimate, "costs"), "costs", dict)
    rows = scenario.get("uavs")
    if not isinstance(rows, list) or len(rows) != len(uids):
        raise AtomicError("ALLOCATION_EVIDENCE_MISMATCH", "原始求解候选记录数量不一致")
    for i, (candidate, row) in enumerate(zip(prepared["candidates"], rows), 1):
        uid = candidate["uav_id"]
        _same(row.get("id"), i, "原始求解机号映射不一致")
        for source, target in (("range_budget", "max_range"), ("time_budget", "max_endurance")):
            _same(row.get(target), candidate[source], "原始求解预算不一致")
        for field in ("transfer_range", "transfer_time"):
            expected = [[None if j == k else (0.0 if v is None else v) for k, v in enumerate(line)]
                        for j, line in enumerate(numeric[field][uid])]
            _same(row.get(field), expected, "原始求解转场成本与当前成本记录不一致")
        _same(row.get("patrol_range_base"), [None] + numeric["work_range"], "原始求解作业航程不一致")
        _same(row.get("patrol_time_base"), [None] + numeric["work_time"], "原始求解作业时间不一致")
    constraints = require(allocation.get("constraints", {}), "constraints", dict)
    fixed = constraints.get("fixed_assignments", {})
    for jid, uid in estimate.get("constraints", {}).get("fixed_assignments", {}).items():
        _same(fixed.get(jid), uid, "分配遗漏已明确的固定归属")
    single = constraints.get("single_owner_jobs", [])
    if not set(estimate.get("constraints", {}).get("single_owner_jobs", [])).issubset(single):
        raise AtomicError("ALLOCATION_EVIDENCE_MISMATCH", "圆形巡逻作业缺少单机归属约束")
    _same(scenario.get("fixed_assignments", {}), {str(jids.index(jid) + 1): uids.index(uid) + 1 for jid, uid in fixed.items()},
          "求解场景固定约束不一致")
    _same(set(scenario.get("single_owner_jobs", [])), {jids.index(jid) + 1 for jid in single}, "求解场景单机约束不一致")
    _same(scenario.get("required_drones"), constraints.get("required_drones"), "求解场景实际出动数约束不一致")
    allowed = require(constraints.get("allowed_assignments", {}), "allowed_assignments", dict)
    for jid, who in allowed.items():
        if (jid not in jids or not isinstance(who, list)
                or any(not isinstance(uid, str) or uid not in uids for uid in who)
                or len(who) != len(set(who))):
            raise AtomicError("ALLOCATION_EVIDENCE_MISMATCH", "分配允许候选约束格式或编号不一致")
    eligible = {str(j + 1): [i + 1 for i, uid in enumerate(uids)
                            if jid in numeric["reachable"][uid] and uid in allowed.get(jid, uids)]
                for j, jid in enumerate(jids)}
    if any(not who for who in eligible.values()):
        raise AtomicError("ALLOCATION_EVIDENCE_MISMATCH", "作业允许候选与物理可达范围交集为空")
    if any(uids.index(uid) + 1 not in eligible[str(jids.index(jid) + 1)] for jid, uid in fixed.items()):
        raise AtomicError("ALLOCATION_EVIDENCE_MISMATCH", "固定归属不在允许候选与物理可达范围交集内")
    _same(scenario.get("eligible_uavs"), eligible, "求解场景候选范围与可达性及允许候选约束不一致")
    try:
        validate_scenario(scenario)
        verify_result(scenario, result)
    except ValueError as exc:
        raise AtomicError("ALLOCATION_EVIDENCE_MISMATCH", "数值求解记录校验失败: " + str(exc)) from exc
    return prepared, check


def _zones(jobs, owners):
    answer = {}
    for job in jobs:
        who = owners[job["job_id"]]
        if len(who) == 1:
            zone = deepcopy(job)
            zone["label"] = "整区"
            answer[(job["job_id"], who[0])] = zone
            continue
        side = 2 * job["radius"] if job["shape"] == "circle" else None
        region = {"shape": "rectangle", "center_longitude": job["center"][0], "center_latitude": job["center"][1],
                  "length_m": side or job["length"], "width_m": side or job["width"]}
        grid = partition_rectangle(region, len(who))
        for cell, uid in zip(grid["zones"], who):
            zone = deepcopy(job)
            zone.update(shape="rectangle", center=[cell["center_longitude"], cell["center_latitude"]],
                        length=cell["length_m"], width=cell["width_m"], label=f"{cell['zone_index']}/{len(who)}")
            zone.pop("radius", None)
            answer[(job["job_id"], uid)] = zone
    return answer


def build(request):
    """Generate final old-schema route steps for the supplied verified assignment."""
    if set(request) - {"operation", "cost_estimate", "allocation", "search_timeout"}:
        raise AtomicError("INVALID_ARGUMENT", "build 输入含不支持字段；参数沿用成本记录")
    estimate = require(required(request, "cost_estimate"), "cost_estimate", dict)
    allocation = require(required(request, "allocation"), "allocation", dict)
    if estimate.get("geometry_model") != GEOMETRY_MODEL or estimate.get("map_used") is not False:
        raise AtomicError("ALLOCATION_EVIDENCE_MISMATCH", "请按当前区域几何重新计算成本和分配")
    prepared, check = _validate_allocation(estimate, allocation)
    timeout = finite(request.get("search_timeout", 1200), "search_timeout", True)
    candidates = {u["uav_id"]: u for u in prepared["candidates"]}
    jobs = {j["job_id"]: j for j in prepared["jobs"]}
    zones = _zones(prepared["jobs"], allocation["owners"])
    assignments = []
    for rank, supplied in enumerate(sorted(allocation["assignments"], key=lambda row: row["uav_id"])):
        uid = supplied["uav_id"]
        candidate = candidates[uid]
        altitude = float(Decimal(str(prepared["flight_alt"])) + Decimal("0.3") * rank)
        maximum = candidate["max_altitude"]
        if altitude > maximum:
            raise AtomicError("ALTITUDE_INFEASIBLE", "错高后超过任务或机型航高上限", uav_id=uid,
                              flight_alt=altitude, max_altitude=maximum)
        ordered = [zones[(jid, uid)] for jid in supplied["job_ids"]]
        if prepared["task_type"] == "patrol" and any(jobs[jid]["shape"] == "circle" and len(allocation["owners"][jid]) != 1
                                                       for jid in supplied["job_ids"]):
            raise AtomicError("ALLOCATION_EVIDENCE_MISMATCH", "圆形巡逻不能分区后代替完整圆周")
        origin = candidate["origin"]
        steps, transit_range, work_range = build_steps(prepared, ordered, origin, altitude, timeout)
        distance = transit_range + work_range
        battery_after = candidate["battery"] * (1 - distance / candidate["flight_remain"])
        duration = transit_range / prepared["speeds"]["transit"] + work_range / prepared["speeds"]["work"]
        if duration > candidate["time_budget"]:
            raise AtomicError("BUDGET_EXCEEDED", "最终分区的估算水平作业时间超过预算",
                              uav_id=uid, estimated_time_s=duration, time_budget=candidate["time_budget"])
        if distance > candidate["range_budget"] or battery_after <= 20:
            raise AtomicError("BUDGET_EXCEEDED", "最终分区航程超过预算或剩余电量不高于20%", uav_id=uid,
                              estimated_range_m=distance, estimated_battery_after=battery_after)
        assignments.append({"uav_id": uid, "origin": deepcopy(origin), "flight_alt": altitude,
                            "estimated_time_s": round(duration, 3),
                            "battery_level": candidate["battery"], "flight_remain": candidate["flight_remain"],
                            "range_budget": candidate["range_budget"], "time_budget": candidate["time_budget"],
                            "jobs": list(supplied["job_ids"]), "estimated_range_m": round(distance, 3),
                            "estimated_battery_after": round(battery_after, 6), "steps": steps})
    report_jobs = []
    for job in prepared["jobs"]:
        row = {key: deepcopy(job[key]) for key in ("job_id", "name", "area_source", "shape", "center") if key in job}
        row.update({key: job[key] for key in ("radius", "length", "width") if key in job})
        row["owners"] = list(allocation["owners"][job["job_id"]])
        report_jobs.append(row)
    output = {"status": "OK", "submitted": False, "task_key": "T" + time.strftime("%m%d%H%M%S") + uuid.uuid4().hex[:4],
              "task_type": prepared["task_type"], "flight_alt": prepared["flight_alt"], "laps": prepared["laps"],
              "speed": prepared.get("requested_speed"), "speeds": prepared["speeds"], "search_timeout": timeout,
              "candidates": [u["uav_id"] for u in prepared["candidates"]], "jobs": report_jobs,
              "assignments": assignments, "unused": deepcopy(allocation["unused"]), "solver": deepcopy(allocation["solver"]),
              "allocation_check": check, "budget_scope": "水平转场和覆盖/巡逻长度估计；不含起降、搜索超时上界或跟踪占用",
              "whole_mission_budget_complete": False}
    output["_summary"] = {"task_key": output["task_key"], "task_type": output["task_type"],
                          "assignments": [{k: row[k] for k in ("uav_id", "flight_alt", "jobs", "battery_level", "flight_remain",
                                                               "range_budget", "time_budget", "estimated_range_m", "estimated_battery_after")}
                                          for row in assignments], "unused": output["unused"],
                          "budget_scope": output["budget_scope"], "submitted": False}
    output.update(geometry_model=GEOMETRY_MODEL, map_used=False, mock=True,
                  map_context={"backend": "mock", "mock": True})
    output["_summary"].update(geometry_model=GEOMETRY_MODEL, map_used=False)
    return output


def compute(request):
    """operation=costs prepares numerical costs; operation=build generates supplied routes."""
    require(request, "input", dict)
    operation = required(request, "operation")
    try:
        if operation == "costs":
            return costs(request)
        if operation == "build":
            return build(request)
        raise AtomicError("INVALID_ARGUMENT", "operation 须为 costs 或 build")
    except (ValueError, OverflowError) as exc:
        raise AtomicError("INVALID_ARGUMENT", str(exc)) from exc
