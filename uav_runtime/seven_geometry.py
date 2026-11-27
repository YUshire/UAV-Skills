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
from uav_runtime.budget_math import DEFAULT_REF_ALT_M, effective_altitude_m
from uav_runtime.direct_path import direct_route
from uav_runtime.planning_geometry import (GEOMETRY_MODEL, build_steps,
                                          candidate_fov_deg, cost_geometry,
                                          job_path_mode, job_radius,
                                          job_search_work_m, region_route,
                                          search_coverage_m, search_lane_m)
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
    altitude_explicit = request.get("flight_alt") is not None
    altitude = finite(request["flight_alt"], "flight_alt", True) if altitude_explicit else DEFAULT_REF_ALT_M
    laps = request.get("laps", 1)
    if type(laps) is not int or laps < 1 or (task_type != "patrol" and laps != 1):
        raise AtomicError("INVALID_ARGUMENT", "laps 须为正整数且只用于巡逻")
    ordered = request.get("ordered", False)
    if type(ordered) is not bool:
        raise AtomicError("INVALID_ARGUMENT", "ordered 须为布尔值")
    return_to_home = request.get("return_to_home", True)
    if type(return_to_home) is not bool:
        raise AtomicError("INVALID_ARGUMENT", "return_to_home 须为布尔值")
    speed = request.get("speed")
    if speed is not None:
        speed = finite(speed, "speed", True)
    req_work_speed = request.get("work_speed")
    if req_work_speed is not None:
        req_work_speed = finite(req_work_speed, "work_speed", True)
    for candidate in candidates:
        uid = candidate["uav_id"]
        for key in ("range_budget", "time_budget", "flight_remain", "battery", "max_altitude"):
            finite(required(candidate, key), uid + "." + key, True)
        origin = require(required(candidate, "origin"), uid + ".origin", dict)
        lon = finite(required(origin, "longitude"), uid + ".longitude")
        lat = finite(required(origin, "latitude"), uid + ".latitude")
        if not -180 <= lon <= 180 or not -90 <= lat <= 90:
            raise AtomicError("INVALID_ARGUMENT", "候选机原点超出经纬度范围")
        if altitude_explicit and altitude > candidate["max_altitude"]:
            raise AtomicError("ALTITUDE_INFEASIBLE", "基准航高超过候选机上限", uav_id=uid)
        candidate["planning_alt"] = effective_altitude_m(
            candidate["max_altitude"], altitude if altitude_explicit else None)
        # Speed is a screening condition, not a fatal input error: a global
        # speed must narrow the candidate pool rather than abort the whole run.
        finite(required(candidate, "max_speed"), uid + ".max_speed", True)
        if candidate.get("max_flight_radius") is not None:
            finite(candidate["max_flight_radius"], uid + ".max_flight_radius", True)
        if candidate.get("reserve_battery_percent") is not None:
            finite(candidate["reserve_battery_percent"], uid + ".reserve_battery_percent", True)
        if speed is not None:
            candidate["planning_speed"] = speed
            candidate["speed_source"] = "user"
        else:
            try:
                candidate["planning_speed"] = finite(candidate.get("cruise_speed"),
                                                     "cruise_speed", positive=True)
                candidate["speed_source"] = "cruise_speed"
            except AtomicError:
                candidate["planning_speed"] = 1.2
                candidate["speed_source"] = "default_1.2"
    min_planning = min(candidate["planning_speed"] for candidate in candidates)
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
        job_type = job.get("task_type") or task_type
        if job_type not in ("search", "patrol"):
            raise AtomicError("INVALID_ARGUMENT", "job.task_type 须为 search 或 patrol", job_id=job["job_id"])
        raw_mode = job.get("path_mode")
        if raw_mode is not None and raw_mode not in ("coverage", "boundary"):
            raise AtomicError("INVALID_ARGUMENT", "job.path_mode 须为 coverage 或 boundary", job_id=job["job_id"])
        job_laps = job.get("laps")
        if job_laps is None:
            job_laps = laps if (raw_mode == "boundary" or (raw_mode is None and job_type == "patrol")) else 1
        if type(job_laps) is not int or job_laps < 1:
            raise AtomicError("INVALID_ARGUMENT", "job.laps 须为正整数", job_id=job["job_id"])
        if raw_mode is None and job_type == "search" and job_laps > 1:
            raw_mode = "boundary"
        effective_mode = raw_mode or ("boundary" if job_type == "patrol" else "coverage")
        if effective_mode == "coverage" and job_laps != 1:
            raise AtomicError("INVALID_ARGUMENT", "覆盖模式不支持 laps > 1；绕飞多圈请设 path_mode=boundary", job_id=job["job_id"])
        job["task_type"] = job_type
        job["path_mode"] = effective_mode
        job["laps"] = job_laps
        fixed_speed = next((candidate["planning_speed"] for candidate in candidates
                            if candidate["uav_id"] == job.get("fixed_uav")), None)
        job["work_speed"] = req_work_speed or speed or fixed_speed or min_planning
        if "search_timeout" in job:
            job["search_timeout"] = finite(job["search_timeout"], "jobs.search_timeout", True)
            if job_type != "search":
                raise AtomicError("INVALID_ARGUMENT", "search_timeout 只用于搜索", job_id=job["job_id"])
    fovs = []
    for candidate in candidates:
        try:
            value = finite(candidate.get("fov"), "fov", positive=True)
        except AtomicError:
            continue
        fovs.append(value)
    default_fov = min(fovs) if fovs else 89.0
    by_id = {candidate["uav_id"]: candidate for candidate in candidates}
    for job in jobs:
        if job_path_mode(job) != "coverage":
            job["fov_deg"] = None
            job["search_lane_m"] = None
            continue
        fov = None
        fixed = job.get("fixed_uav")
        if fixed and fixed in by_id:
            try:
                fov = finite(by_id[fixed].get("fov"), "fov", positive=True)
            except AtomicError:
                fov = None
        if fov is None:
            fov = default_fov
        job["fov_deg"] = fov
        job["search_lane_m"] = search_lane_m(altitude, fov)
    planning_speeds = [candidate["planning_speed"] for candidate in candidates]
    return {"candidates": candidates, "jobs": jobs, "task_type": task_type,
            "flight_alt": altitude, "altitude_explicit": altitude_explicit,
            "laps": laps, "ordered": ordered,
            "return_to_home": return_to_home,
            "speeds": {"transit": min(planning_speeds), "work": req_work_speed or max(planning_speeds)},
            "requested_speed": speed, "requested_work_speed": req_work_speed}


_SCREENING_NEAREST_LIMIT = 5
_SCREENING_SAMPLE_LIMIT = 20
_ALTERNATIVES_INPUT_LIMIT = 64
_ALTERNATIVES_OUTPUT_LIMIT = 10


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
             "farthest_m": bound.get("farthest_m"),
             "max_flight_radius_m": candidate.get("max_flight_radius"),
             "radius_ok": bound.get("radius_ok", True),
             "speed_ok": item.get("speed_ok", True),
             "max_speed_mps": candidate.get("max_speed"),
             "planning_speed_mps": candidate.get("planning_speed"),
             "speed_source": candidate.get("speed_source"),
             "requested_transit_speed_mps": item.get("requested_transit_speed_mps"),
             "requested_work_speed_mps": item.get("requested_work_speed_mps")}
    cruise = candidate.get("cruise_speed")
    if isinstance(cruise, (int, float)) and not isinstance(cruise, bool) and cruise > 0:
        cruise_time = bound["round_trip_lower_bound_m"] / cruise
        entry["cruise_speed_mps"] = cruise
        entry["time_lower_bound_at_cruise_s"] = cruise_time
        entry["time_ok_at_cruise"] = cruise_time <= item["time_budget"] + 1e-6
    return entry


def _zero_retained_diagnosis(nearest):
    """Explain the single most promising excluded candidate in actionable terms."""
    if not nearest:
        return {"binding_constraint": "NO_CANDIDATE",
                "actionable": "没有可用于比对的候选机。"}
    top = nearest[0]
    planning_speed = top.get("planning_speed_mps") or top.get("requested_transit_speed_mps")
    if not top.get("radius_ok", True):
        binding = "FLIGHT_RADIUS"
        also_range = ("，且往返航程下界超过预算" if not top["range_ok"] else "")
        actionable = (f"最近候选 {top['uav_id']} 距目标中心 {top['distance_m']:.0f} m，"
                      f"最远作业点 {top['farthest_m']:.0f} m 超过最大飞行半径 "
                      f"{top['max_flight_radius_m']:.0f} m{also_range}；提高速度不能解决，"
                      f"需要更近的起飞点或最大飞行半径更大的机型。")
    elif not top.get("speed_ok", True):
        binding = "SPEED_LIMIT"
        actionable = (f"最近候选 {top['uav_id']} 最大速度 {top['max_speed_mps']:g} m/s 低于规划速度 "
                      f"{planning_speed:g} m/s；可降低速度，或改用支持该速度的机型。")
    elif top["range_ok"] and not top["time_ok"]:
        binding = "TIME_LOWER_BOUND"
        if top.get("time_ok_at_cruise") and top.get("cruise_speed_mps"):
            actionable = (f"最近候选 {top['uav_id']} 距目标中心 {top['distance_m']:.0f} m，"
                          f"往返航程下界 {top['round_trip_lower_bound_m']:.0f} m 未超过航程预算 "
                          f"{top['range_budget']:.0f} m；但按规划速度 {planning_speed:g} m/s 往返需 "
                          f"{top['time_lower_bound_s']:.0f} s，超过时间预算 {top['time_budget']:.0f} s。"
                          f"按其巡航速度 {top['cruise_speed_mps']:g} m/s 约需 "
                          f"{top['time_lower_bound_at_cruise_s']:.0f} s。可指定不超过机型 max_speed 的"
                          f"速度后重算（指定全局速度时需把候选范围缩小到支持该速度的机型），"
                          f"不能笼统判为航程不足。")
        else:
            actionable = (f"最近候选 {top['uav_id']} 距目标中心 {top['distance_m']:.0f} m，"
                          f"往返航程下界 {top['round_trip_lower_bound_m']:.0f} m 未超过航程预算 "
                          f"{top['range_budget']:.0f} m；但按规划速度 {planning_speed:g} m/s 往返需 "
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
    rows = []
    for candidate in prepared["candidates"]:
        uid = candidate["uav_id"]
        origin = candidate["origin"]
        limit = candidate.get("max_flight_radius")
        planning_speed = candidate["planning_speed"]
        for job_id, job in zip(job_ids, prepared["jobs"]):
            distance = direct_route((origin["longitude"], origin["latitude"]),
                                    job["center"], prepared["flight_alt"], points=0)["one_way_distance"]
            round_range = 2 * distance
            round_time = round_range / planning_speed
            farthest = distance + job_radius(job)
            radius_ok = limit is None or farthest <= limit + 1e-6
            rows.append((round_range, round_time, uid, job_id, candidate,
                         radius_ok, farthest, limit))
    if not rows:
        return {"binding_constraint": "NO_CANDIDATE",
                "actionable": "没有可用于比对的候选机。"}
    (round_range, round_time, uid, job_id, candidate,
     radius_ok, farthest, limit) = min(rows, key=lambda row: (row[0], row[1]))
    range_ok = round_range <= candidate["range_budget"] + 1e-6
    time_ok = round_time <= candidate["time_budget"] + 1e-6
    if not radius_ok:
        binding = "FLIGHT_RADIUS"
        actionable = (f"最近候选 {uid} 最远作业点 {farthest:.0f} m 超过最大飞行半径 "
                      f"{limit:.0f} m；提高速度不能解决，需要更近的起飞点或更大半径的机型。")
    else:
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


def _speed_alternatives(prepared, routes, work_range, plausible, skip_ids):
    """Name aircraft that fail only because the planned speed is too low.

    A global speed replaces both transit and work speed, so the minimum speed
    that can fit the time budget is total horizontal distance / time budget.
    The result is evidence for a conditional suggestion, not a silent change.
    """
    rows = []
    requested = prepared.get("requested_speed")
    for candidate in plausible:
        uid = candidate["uav_id"]
        if uid in skip_ids:
            continue
        origin = candidate["origin"]
        home = (origin["longitude"], origin["latitude"])
        planning_speed = candidate["planning_speed"]
        max_speed = candidate["max_speed"]
        radius_limit = candidate.get("max_flight_radius")
        best = None
        for index, job in enumerate(prepared["jobs"]):
            waypoints = routes[index]["waypoints"]
            start, end = waypoints[0], waypoints[-1]
            outbound = direct_route(home, (start["longitude"], start["latitude"]),
                                    prepared["flight_alt"], points=0)["one_way_distance"]
            back = (direct_route((end["longitude"], end["latitude"]), home,
                                 prepared["flight_alt"], points=0)["one_way_distance"]
                    if prepared.get("return_to_home", True) else 0.0)
            transit = outbound + back
            total = transit + work_range[index]
            distance = direct_route(home, job["center"], prepared["flight_alt"],
                                    points=0)["one_way_distance"]
            farthest = distance + job_radius(job)
            radius_ok = radius_limit is None or farthest <= radius_limit + 1e-6
            range_ok = total <= candidate["range_budget"] + 1e-6
            required = (total / candidate["time_budget"]
                        if candidate["time_budget"] > 0 else math.inf)
            speed_ok = required <= max_speed + 1e-9
            default_speed_ok = planning_speed <= max_speed + 1e-9
            default_time = total / planning_speed
            default_time_ok = default_time <= candidate["time_budget"] + 1e-6
            if range_ok and radius_ok and default_time_ok and default_speed_ok:
                continue
            feasible = range_ok and radius_ok and speed_ok
            if not range_ok:
                binding = "RANGE"
            elif not radius_ok:
                binding = "FLIGHT_RADIUS"
            elif not speed_ok:
                binding = "MAX_SPEED"
            else:
                binding = None
            if not range_ok:
                default_binding = "RANGE"
            elif not radius_ok:
                default_binding = "FLIGHT_RADIUS"
            elif not default_speed_ok:
                default_binding = "REQUESTED_SPEED" if requested is not None else "CRUISE_SPEED"
            elif not default_time_ok:
                default_binding = "DEFAULT_TIME"
            else:
                default_binding = None
            suggested = (min(max_speed, math.ceil(required * 1000) / 1000)
                         if feasible else None)
            row = {"uav_id": uid, "job_id": job["job_id"],
                   "distance_to_center_m": round(distance, 3),
                   "transit_range_m": round(transit, 3),
                   "work_range_m": round(work_range[index], 3),
                   "total_range_m": round(total, 3),
                   "range_budget": candidate["range_budget"],
                   "time_budget": candidate["time_budget"],
                   "planning_speed_mps": planning_speed,
                   "speed_source": candidate.get("speed_source"),
                   "default_transit_speed_mps": planning_speed,
                   "default_work_speed_mps": planning_speed,
                   "default_time_s": round(default_time, 3),
                   "default_binding": default_binding,
                   "required_speed_mps": round(required, 3),
                   "max_speed_mps": max_speed,
                   "suggested_speed_mps": suggested,
                   "feasible_with_speed": feasible,
                   "binding": binding,
                   "farthest_m": round(farthest, 3),
                   "max_flight_radius_m": radius_limit,
                   "radius_ok": radius_ok}
            key = (0 if feasible else 1, required if feasible else total, total)
            if best is None or key < best[0]:
                best = (key, row)
        if best is not None:
            rows.append(best[1])
    rows.sort(key=lambda row: (not row["feasible_with_speed"],
                               row["suggested_speed_mps"] if row["feasible_with_speed"]
                               else row["total_range_m"]))
    feasible_count = sum(1 for row in rows if row["feasible_with_speed"])
    return {"scope": "per_job_lower_bound",
            "assumed_global_speed": True,
            "feasible_count": feasible_count,
            "no_speed_alternative_count": len(rows) - feasible_count,
            "items": rows[:_ALTERNATIVES_OUTPUT_LIMIT],
            "omitted": max(0, len(rows) - _ALTERNATIVES_OUTPUT_LIMIT)}


def _candidate_work_range(job, altitude, fov, base_range):
    """Work distance for one candidate at one altitude.

    Coverage depends on the candidate's own FOV; boundary loops are unchanged.
    """
    if job_path_mode(job) != "coverage":
        return base_range
    return job_search_work_m(job, search_lane_m(altitude, fov))


def _task_probe(prepared, candidate, altitude, transfer_range):
    """Best single-job range/time check for one candidate at one altitude."""
    uid = candidate["uav_id"]
    speed = candidate["planning_speed"]
    w_speed = prepared.get("requested_work_speed") or speed
    fov = candidate_fov_deg(candidate, prepared["jobs"][0].get("fov_deg") or 89.0)
    rows = []
    for index, job in enumerate(prepared["jobs"]):
        work = _candidate_work_range(
            job, altitude, fov,
            region_route(job, altitude, job["task_type"])["path_length"] * job["laps"])
        transfer = transfer_range[uid][0][index + 1] + transfer_range[uid][index + 1][0]
        total = transfer + work
        duration = transfer / speed + work / w_speed
        origin = candidate["origin"]
        center_distance = direct_route((origin["longitude"], origin["latitude"]),
                                       job["center"], altitude, points=0)["one_way_distance"]
        radius_ok = (candidate.get("max_flight_radius") is None
                     or center_distance + job_radius(job)
                     <= candidate["max_flight_radius"] + 1e-6)
        row = {"uav_id": uid, "job_id": job["job_id"], "altitude_m": altitude,
               "fov_deg": fov, "work_range_m": round(work, 3),
               "transit_range_m": round(transfer, 3), "total_range_m": round(total, 3),
               "total_time_s": round(duration, 3),
               "range_budget": candidate["range_budget"],
               "time_budget": candidate["time_budget"],
               "range_ok": total <= candidate["range_budget"] + 1e-6,
               "time_ok": duration <= candidate["time_budget"] + 1e-6,
               "radius_ok": radius_ok,
               "battery_after": candidate["battery"] * (1 - total / candidate["flight_remain"])}
        row["feasible"] = row["range_ok"] and row["time_ok"] and row["radius_ok"]
        rows.append(row)
    if not rows:
        return None
    return min(rows, key=lambda row: (not row["feasible"], row["total_range_m"]))


def _multi_job_probe(prepared, candidate, altitude, transfer_range):
    """Single-aircraft multi-job route check (H -> J1 -> ... -> Jn -> H) at one altitude."""
    uid = candidate["uav_id"]
    speed = candidate["planning_speed"]
    w_speed = prepared.get("requested_work_speed") or speed
    fov = candidate_fov_deg(candidate, prepared["jobs"][0].get("fov_deg") or 89.0)
    n = len(prepared["jobs"])
    mat = transfer_range[uid]
    # Check if every job is individually reachable and legs exist.
    order = list(range(1, n + 1))
    transit = 0.0
    prev = 0
    for node in order:
        leg = mat[prev][node]
        if leg is None:
            return None
        transit += leg
        prev = node
    if prepared.get("return_to_home", True):
        back = mat[prev][0]
        if back is None:
            return None
        transit += back
    work = 0.0
    origin = candidate["origin"]
    radius_ok = True
    for job in prepared["jobs"]:
        base = region_route(job, altitude, job["task_type"])["path_length"] * job["laps"] if job_path_mode(job) != "coverage" else 0.0
        work += _candidate_work_range(job, altitude, fov, base)
        center_distance = direct_route((origin["longitude"], origin["latitude"]),
                                       job["center"], altitude, points=0)["one_way_distance"]
        if candidate.get("max_flight_radius") is not None and center_distance + job_radius(job) > candidate["max_flight_radius"] + 1e-6:
            radius_ok = False
    total = transit + work
    duration = transit / speed + work / w_speed
    row = {"uav_id": uid, "job_id": "+".join(j["job_id"] for j in prepared["jobs"]),
           "altitude_m": altitude, "fov_deg": fov,
           "work_range_m": round(work, 3), "transit_range_m": round(transit, 3),
           "total_range_m": round(total, 3), "total_time_s": round(duration, 3),
           "range_budget": candidate["range_budget"], "time_budget": candidate["time_budget"],
           "range_ok": total <= candidate["range_budget"] + 1e-6,
           "time_ok": duration <= candidate["time_budget"] + 1e-6,
           "radius_ok": radius_ok,
           "battery_after": candidate["battery"] * (1 - total / candidate["flight_remain"])}
    row["feasible"] = row["range_ok"] and row["time_ok"] and row["radius_ok"]
    return row


def _altitude_alternatives(prepared, candidates, transfer_range, multi_job=False):
    """Find the lowest higher altitude that makes each candidate feasible."""
    default = prepared["flight_alt"]
    items = []
    probe_fn = _multi_job_probe if multi_job else _task_probe
    for candidate in candidates:
        max_alt = candidate.get("max_altitude", default)
        cand_alts = [a for a in (2.0, 3.0, 5.0, 10.0, 20.0, 50.0) if default < a <= max_alt]
        if default < max_alt and max_alt not in cand_alts and max_alt < 50.0:
            cand_alts.append(max_alt)
        for altitude in cand_alts:
            row = probe_fn(prepared, candidate, altitude, transfer_range)
            if row and row["feasible"]:
                items.append(row)
                break
    items.sort(key=lambda row: (row["altitude_m"], row["total_range_m"]))
    return {"default_altitude_m": default,
            "feasible_count": len(items),
            "items": items[:10],
            "note": "提高航高可缩短搜索航程；识别能力需按设备规格或实测确认。"}


def _screen_candidates(prepared, routes, work_range):
    """Prune only aircraft unable to reach any job area and return.

    A circumscribed disk encloses every possible rectangle partition. Omitting
    work cost keeps this a necessary bound even when a job is shared. Use the
    each candidate's planned speed for the time lower bound.

    The returned screening block distinguishes the range bound from the time
    bound, keeps a compact closest-candidate diagnosis, and returns only a
    capped per-aircraft sample so the tool result stays small enough to be read
    without truncation. It also reports bounded-speed alternatives so a
    default-speed failure is not mistaken for physical unreachability.
    """
    retained, excluded, fixed_infeasible = [], [], []
    plausible = []
    planning_speeds = [candidate["planning_speed"] for candidate in prepared["candidates"]]
    min_planning = min(planning_speeds)
    max_planning = max(planning_speeds)
    for candidate in prepared["candidates"]:
        origin = candidate["origin"]
        planning_speed = candidate["planning_speed"]
        speed_ok = planning_speed <= candidate["max_speed"] + 1e-9
        radius_limit = candidate.get("max_flight_radius")
        bounds = []
        for job in prepared["jobs"]:
            radius = job_radius(job)
            distance = direct_route((origin["longitude"], origin["latitude"]),
                                    job["center"], prepared["flight_alt"], points=0)["one_way_distance"]
            farthest = distance + radius
            radius_ok = radius_limit is None or farthest <= radius_limit + 1e-6
            # direct_route rounds to millimetres; bias downward to preserve a lower bound.
            leg_factor = 2 if prepared.get("return_to_home", True) else 1
            lower = leg_factor * max(0.0, distance - radius - 0.001)
            bound = {"job_id": job["job_id"], "distance_m": distance,
                     "farthest_m": farthest, "max_flight_radius_m": radius_limit,
                     "radius_ok": radius_ok,
                     "round_trip_lower_bound_m": lower,
                     "time_lower_bound_s": lower / planning_speed,
                     "range_ok": lower <= candidate["range_budget"] + 1e-6,
                     "time_ok": lower / planning_speed <= candidate["time_budget"] + 1e-6,
                     "speed_ok": speed_ok, "work_speed_mps": planning_speed}
            bounds.append(bound)
            if job.get("fixed_uav") == candidate["uav_id"] and not (
                    speed_ok and bound["range_ok"] and bound["time_ok"] and radius_ok):
                fixed_infeasible.append(dict(bound, uav_id=candidate["uav_id"],
                                             range_budget=candidate["range_budget"],
                                             time_budget=candidate["time_budget"],
                                             speed_ok=speed_ok,
                                             max_speed_mps=candidate["max_speed"],
                                             requested_transit_speed_mps=planning_speed,
                                             requested_work_speed_mps=planning_speed))
        # Only candidates whose optimistic lower bound already fits the range,
        # radius and a speed within max_speed can become feasible by changing speed.
        if any(b["range_ok"] and b["radius_ok"]
               and b["round_trip_lower_bound_m"] / candidate["max_speed"]
               <= candidate["time_budget"] + 1e-6 for b in bounds):
            plausible.append((min(b["round_trip_lower_bound_m"] for b in bounds), candidate))
        usable = [b for b in bounds if b["speed_ok"] and b["radius_ok"]]
        if any(b["range_ok"] and b["time_ok"] for b in usable):
            retained.append(candidate)
            continue
        best = min(usable or bounds,
                   key=lambda b: (b["round_trip_lower_bound_m"], b["time_lower_bound_s"]))
        if not usable:
            if not best["radius_ok"]:
                reason = "FLIGHT_RADIUS"
                failed = ["radius"] + [name for name in ("range", "time") if not best[name + "_ok"]]
            else:
                reason = "SPEED_LIMIT"
                failed = ["speed"] + [name for name in ("range", "time") if not best[name + "_ok"]]
        else:
            reason, failed = _exclusion_reason(best)
        excluded.append({"uav_id": candidate["uav_id"], "reason": reason,
                         "failed_bounds": failed,
                         "range_budget": candidate["range_budget"],
                         "time_budget": candidate["time_budget"],
                         "speed_ok": best["speed_ok"],
                         "max_speed_mps": candidate["max_speed"],
                         "requested_transit_speed_mps": planning_speed,
                         "requested_work_speed_mps": best["work_speed_mps"],
                         "best_bound": best})

    plausible.sort(key=lambda item: item[0])
    plausible = [candidate for _, candidate in plausible[:_ALTERNATIVES_INPUT_LIMIT]]
    reasons = {"RANGE_LOWER_BOUND": 0, "TIME_LOWER_BOUND": 0, "BOTH_LOWER_BOUNDS": 0,
               "SPEED_LIMIT": 0, "FLIGHT_RADIUS": 0}
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
                 "retained_count": len(retained),
                 "retained_uav_ids": [u["uav_id"] for u in retained[:15]],
                 "excluded_count": len(excluded),
                 "assumed_transit_speed_mps": min_planning,
                 "transit_speed_mps": min_planning,
                 "work_speed_mps": max_planning,
                 "requested_speed_mps": prepared.get("requested_speed"),
                 "reasons": reasons,
                 "nearest": nearest,
                 "range_ok_but_time_fail": [item["uav_id"] for item in ordered
                                            if item.get("speed_ok", True)
                                            and item["best_bound"]["range_ok"]
                                            and not item["best_bound"]["time_ok"]],
                 "excluded": ordered[:_SCREENING_SAMPLE_LIMIT],
                 "excluded_omitted": max(0, len(excluded) - _SCREENING_SAMPLE_LIMIT)}
    screening["alternatives"] = _speed_alternatives(
        prepared, routes, work_range, plausible, {u["uav_id"] for u in retained})
    if not retained:
        screening["diagnosis"] = _zero_retained_diagnosis(nearest)
    retained_ids = {u["uav_id"] for u in retained}
    fixed_missing = {j["job_id"]: j["fixed_uav"] for j in prepared["jobs"]
                     if j.get("fixed_uav") and j["fixed_uav"] not in retained_ids}
    if not retained or fixed_missing or fixed_infeasible:
        diagnosis = screening.get("diagnosis")
        if diagnosis:
            message = "没有可行候选机；" + diagnosis["actionable"]
        else:
            message = "往返航程/时间下界已超限，或固定执行机不在可用候选中"
        alternatives = screening["alternatives"]
        message = message.rstrip("。")
        if alternatives["feasible_count"]:
            top = alternatives["items"][0]
            message += (f"；另有 {alternatives['feasible_count']} 架候选在指定速度后可完成对应作业，"
                        f"例如 {top['uav_id']} 需不低于 {top['suggested_speed_mps']:g} m/s"
                        f"（上限 {top['max_speed_mps']:g} m/s）。")
        else:
            message += "；当前候选池内没有候选机能在其最大速度、航程和飞行半径内完成任务。"
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
    if set(request) - {"operation", "candidates", "jobs", "task_type", "flight_alt", "laps", "speed", "work_speed", "ordered", "return_to_home"}:
        raise AtomicError("INVALID_ARGUMENT", "costs 输入含不支持字段")
    prepared = _prepared(request, candidate_limit=4096)
    routes = [region_route(job, prepared["flight_alt"], job["task_type"],
                           job.get("search_lane_m")) for job in prepared["jobs"]]
    work_range = []
    for route, job in zip(routes, prepared["jobs"]):
        if job_path_mode(job) == "coverage":
            work_range.append(job_search_work_m(job, job.get("search_lane_m") or 2.0))
        else:
            work_range.append(route["path_length"] * job["laps"])
    screening = _screen_candidates(prepared, routes, work_range)
    answer = cost_geometry(prepared, routes, work_range)
    reachable = answer["costs"]["reachable"]
    kept = [candidate for candidate in prepared["candidates"]
            if reachable.get(candidate["uav_id"])]
    if not kept:
        diagnosis = screening.get("diagnosis") or _actual_reach_diagnosis(
            prepared, answer["job_ids"])
        screening["reachable_count"] = 0
        message = "按实际往返转场，没有候选机能到达任何作业；" + diagnosis["actionable"]
        alternatives = screening.get("alternatives", {})
        if alternatives.get("feasible_count"):
            top = alternatives["items"][0]
            message += (f"；另有 {alternatives['feasible_count']} 架候选在指定速度后可完成对应作业，"
                        f"例如 {top['uav_id']} 需不低于 {top['suggested_speed_mps']:g} m/s"
                        f"（上限 {top['max_speed_mps']:g} m/s）。")
        raise AtomicError("NO_FEASIBLE_ALLOCATION", message,
                          candidate_screening=screening)
    if len(kept) != len(prepared["candidates"]):
        screening["cost_excluded_count"] = len(prepared["candidates"]) - len(kept)
        prepared["candidates"] = kept
        for key in ("transfer_range", "transfer_time", "reachable", "work_range_by_uav", "work_time_by_uav"):
            if key in answer["costs"]:
                answer["costs"][key] = {candidate["uav_id"]: answer["costs"][key][candidate["uav_id"]]
                                        for candidate in kept}
    screening["reachable_count"] = len(kept)
    transfer_range = answer["costs"]["transfer_range"]
    if kept and len(prepared["jobs"]) == 1:
        probes = [_task_probe(prepared, candidate, candidate.get("planning_alt", prepared["flight_alt"]), transfer_range)
                  for candidate in kept]
        screening["task_feasibility"] = probes
        if not any(probe and probe["feasible"] for probe in probes):
            altitude = _altitude_alternatives(prepared, kept, transfer_range)
            screening["altitude_alternatives"] = altitude
            best = altitude["items"][0] if altitude["feasible_count"] else None
            job_name = prepared["jobs"][0].get("name", prepared["jobs"][0]["job_id"])
            message = (f"按当前航高 {prepared['flight_alt']:g} m，没有候选机能完成"
                       f" {job_name}。")
            if best:
                message += (f"可提高到 {best['altitude_m']:g} m：{best['uav_id']} 预计总航程"
                            f" {best['total_range_m']:.0f} m、时间 {best['total_time_s']:.0f} s、"
                            f"剩余电量 {best['battery_after']:.1f}%；提高航高后的识别能力"
                            f"需设备规格或实测确认。")
            else:
                message += "提高到记录中的最大航高仍不可行。"
            raise AtomicError("NO_FEASIBLE_ALLOCATION", message,
                              candidate_screening=screening)
    elif kept and len(kept) == 1 and len(prepared["jobs"]) > 1 and any(job_path_mode(j) == "coverage" for j in prepared["jobs"]):
        probe = _multi_job_probe(prepared, kept[0], kept[0].get("planning_alt", prepared["flight_alt"]), transfer_range)
        if probe and not probe["feasible"]:
            altitude = _altitude_alternatives(prepared, kept, transfer_range, multi_job=True)
            screening["altitude_alternatives"] = altitude
            best = altitude["items"][0] if altitude["feasible_count"] else None
            message = f"按当前航高 {prepared['flight_alt']:g} m，唯一可达候选 {kept[0]['uav_id']} 无法单趟完成全部连续作业。"
            if best:
                message += (f"可提高到 {best['altitude_m']:g} m：{best['uav_id']} 预计总航程"
                            f" {best['total_range_m']:.0f} m、时间 {best['total_time_s']:.0f} s、"
                            f"剩余电量 {best['battery_after']:.1f}%；提高航高后的识别能力"
                            f"需设备规格或实测确认。")
            else:
                message += "提高到 50 m（或机型升限）仍不可行。"
            raise AtomicError("NO_FEASIBLE_ALLOCATION", message,
                              candidate_screening=screening)
    if len(kept) == 1:
        candidate = kept[0]
        uid = candidate["uav_id"]
        cand_alt = candidate.get("planning_alt", prepared["flight_alt"])
        cand_work_speed = prepared.get("requested_work_speed") or candidate["planning_speed"]
        fov = candidate_fov_deg(candidate, prepared["jobs"][0].get("fov_deg") or 89.0)
        for job in prepared["jobs"]:
            is_cov = job_path_mode(job) == "coverage"
            job["fov_deg"] = fov if is_cov else None
            job["search_lane_m"] = search_lane_m(cand_alt, fov) if is_cov else None
            job["work_speed"] = cand_work_speed
        updated_routes = [region_route(job, cand_alt, job["task_type"],
                                       job.get("search_lane_m"))
                          for job in prepared["jobs"]]
        updated_work = [_candidate_work_range(
            job, cand_alt, fov,
            route["path_length"] * job["laps"])
            for route, job in zip(updated_routes, prepared["jobs"])]
        answer["costs"]["work_range"] = updated_work
        answer["costs"]["work_time"] = [work / cand_work_speed for work in updated_work]
        answer["costs"]["work_range_by_uav"][uid] = list(updated_work)
        answer["costs"]["work_time_by_uav"][uid] = list(answer["costs"]["work_time"])
    fixed = {job["job_id"]: job["fixed_uav"] for job in prepared["jobs"] if job.get("fixed_uav")}
    # Circular boundary loops must stay with one aircraft to preserve the complete ring.
    single = [job["job_id"] for job in prepared["jobs"]
              if job["shape"] == "circle" and job_path_mode(job) == "boundary"]
    constraints = {}
    if fixed:
        constraints["fixed_assignments"] = fixed
    if single:
        constraints["single_owner_jobs"] = single
    task_types = sorted({job["task_type"] for job in prepared["jobs"]})
    output = {"status": "OK", "prepared": prepared, "prepared_digest": digest(prepared),
              "candidates": deepcopy(prepared["candidates"]),
              "jobs": [{"job_id": j["job_id"], "name": j.get("name", j["job_id"]),
                        "task_type": j["task_type"], "laps": j["laps"]} for j in prepared["jobs"]],
              "task_type": task_types[0] if len(task_types) == 1 else "mixed",
              "task_types": task_types, "laps": prepared["laps"],
              "ordered": prepared["ordered"], "costs": answer["costs"], "constraints": constraints,
              "candidate_screening": screening, "submitted": False}
    output["_summary"] = {"job_ids": answer["job_ids"], "candidate_ids": [u["uav_id"] for u in prepared["candidates"]],
                          "constraints": constraints, "ordered": prepared["ordered"],
                          "task_types": task_types, "estimated_only": True,
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
    simple_jobs = [{"job_id": j["job_id"], "name": j.get("name", j["job_id"]),
                    "task_type": j["task_type"], "laps": j["laps"]} for j in prepared["jobs"]]
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
        expected_work = (numeric["work_range_by_uav"][uid]
                         if isinstance(numeric.get("work_range_by_uav"), dict) and uid in numeric["work_range_by_uav"]
                         else numeric["work_range"])
        _same(row.get("patrol_range_base"), [None] + expected_work, "原始求解作业航程不一致")
        speed = candidate.get("planning_speed")
        if isinstance(numeric.get("work_time_by_uav"), dict) and uid in numeric["work_time_by_uav"]:
            expected_time = [None] + numeric["work_time_by_uav"][uid]
        elif speed:
            expected_time = [None] + [w / speed for w in expected_work]
        else:
            expected_time = [None] + numeric["work_time"]
        _same(row.get("patrol_time_base"), expected_time, "原始求解作业时间不一致")
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
    if set(request) - {"operation", "cost_estimate", "allocation", "search_timeout", "step_metrics", "altitude_order"}:
        raise AtomicError("INVALID_ARGUMENT", "build 输入含不支持字段；参数沿用成本记录")
    if type(request.get("step_metrics", False)) is not bool:
        raise AtomicError("INVALID_ARGUMENT", "step_metrics 须为布尔值")
    estimate = require(required(request, "cost_estimate"), "cost_estimate", dict)
    allocation = require(required(request, "allocation"), "allocation", dict)
    if estimate.get("geometry_model") != GEOMETRY_MODEL or estimate.get("map_used") is not False:
        raise AtomicError("ALLOCATION_EVIDENCE_MISMATCH", "请按当前区域几何重新计算成本和分配")
    prepared, check = _validate_allocation(estimate, allocation)
    timeout = finite(request.get("search_timeout", 1200), "search_timeout", True)
    candidates = {u["uav_id"]: u for u in prepared["candidates"]}
    jobs = {j["job_id"]: j for j in prepared["jobs"]}
    zones = _zones(prepared["jobs"], allocation["owners"])
    altitude_order = request.get("altitude_order")
    if altitude_order is not None:
        if (not isinstance(altitude_order, list) or not 1 <= len(altitude_order) <= 64
                or any(not isinstance(uid, str) or not uid for uid in altitude_order)
                or len(set(altitude_order)) != len(altitude_order)
                or not {row["uav_id"] for row in allocation["assignments"]} <= set(altitude_order)):
            raise AtomicError("INVALID_ARGUMENT", "altitude_order须为覆盖参与机的唯一机号列表，最多64架")
    assignments = []
    for rank, supplied in enumerate(sorted(allocation["assignments"], key=lambda row: row["uav_id"])):
        uid = supplied["uav_id"]
        candidate = candidates[uid]
        height_rank = altitude_order.index(uid) if altitude_order is not None else rank
        base_alt = candidate.get("planning_alt", prepared["flight_alt"])
        altitude = float(Decimal(str(base_alt)) + Decimal("0.3") * height_rank)
        maximum = candidate["max_altitude"]
        if altitude > maximum:
            if not prepared.get("altitude_explicit", False):
                stepped_down = float(Decimal(str(maximum)) - Decimal("0.3") * height_rank)
                if stepped_down > 0.3:
                    altitude = stepped_down
            if altitude > maximum:
                raise AtomicError("ALTITUDE_INFEASIBLE", "错高后超过任务或机型航高上限", uav_id=uid,
                                  flight_alt=altitude, max_altitude=maximum)
        job_order = {job["job_id"]: index for index, job in enumerate(prepared["jobs"])}
        supplied_jobs = (sorted(supplied["job_ids"], key=lambda jid: job_order[jid])
                         if prepared["ordered"] else list(supplied["job_ids"]))
        ordered = [zones[(jid, uid)] for jid in supplied_jobs]
        if any(jobs[jid]["shape"] == "circle" and job_path_mode(jobs[jid]) == "boundary"
               and len(allocation["owners"][jid]) != 1 for jid in supplied_jobs):
            raise AtomicError("ALLOCATION_EVIDENCE_MISMATCH", "圆形边界不能分区后代替完整圆周")
        origin = candidate["origin"]
        speed = candidate["planning_speed"]
        cand_fov = candidate_fov_deg(candidate, prepared["jobs"][0].get("fov_deg") or 89.0)
        steps, transit_range, work_range, work_time = build_steps(
            prepared, ordered, origin, altitude, timeout, work_speed=prepared.get("requested_work_speed") or speed,
            include_metrics=request.get("step_metrics", False), candidate_fov=cand_fov, transit_speed=speed)
        distance = transit_range + work_range
        battery_after = candidate["battery"] * (1 - distance / candidate["flight_remain"])
        duration = transit_range / speed + work_time
        if duration > candidate["time_budget"] + 1e-6:
            raise AtomicError("BUDGET_EXCEEDED", "最终分区的估算水平作业时间超过预算",
                              uav_id=uid, estimated_time_s=duration, time_budget=candidate["time_budget"])
        reserve = candidate.get("reserve_battery_percent", 20)
        if distance > candidate["range_budget"] + 1e-6 or battery_after < reserve - 1e-9:
            raise AtomicError("BUDGET_EXCEEDED", "最终分区航程超过预算或剩余电量低于返航预留", uav_id=uid,
                              estimated_range_m=distance, estimated_battery_after=battery_after,
                              reserve_battery_percent=reserve)
        assignments.append({"uav_id": uid, "speed": speed, "speed_source": candidate.get("speed_source"),
                            "origin": deepcopy(origin), "flight_alt": altitude,
                            "estimated_time_s": round(duration, 3),
                            "battery_level": candidate["battery"], "flight_remain": candidate["flight_remain"],
                            "range_budget": candidate["range_budget"], "time_budget": candidate["time_budget"],
                            "jobs": supplied_jobs, "estimated_range_m": round(distance, 3),
                            "estimated_battery_after": round(battery_after, 6), "steps": steps})
    report_jobs = []
    for job in prepared["jobs"]:
        row = {key: deepcopy(job[key]) for key in ("job_id", "name", "area_source", "shape", "center") if key in job}
        row.update({key: job[key] for key in ("radius", "length", "width") if key in job})
        row.update({key: job[key] for key in ("task_type", "laps", "search_timeout") if key in job})
        row["owners"] = list(allocation["owners"][job["job_id"]])
        report_jobs.append(row)
    task_types = sorted({job["task_type"] for job in prepared["jobs"]})
    rth = prepared.get("return_to_home", True)
    budget_scope = ("水平转场和覆盖/巡逻长度估计；不含起降、搜索超时上界或跟踪占用" if rth
                    else "单程水平转场和覆盖/巡逻长度估计（不返航，结束后原地悬停）；不含起降与搜索超时上界")
    output = {"status": "OK", "submitted": False, "task_key": "T" + time.strftime("%m%d%H%M%S") + uuid.uuid4().hex[:4],
              "task_type": task_types[0] if len(task_types) == 1 else "mixed",
              "task_types": task_types, "flight_alt": prepared["flight_alt"], "laps": prepared["laps"],
              "ordered": prepared["ordered"], "return_to_home": rth,
              "speed": prepared.get("requested_speed"), "speeds": prepared["speeds"], "search_timeout": timeout,
              "candidates": [u["uav_id"] for u in prepared["candidates"]], "jobs": report_jobs,
              "assignments": assignments, "unused": deepcopy(allocation["unused"]), "solver": deepcopy(allocation["solver"]),
              "allocation_check": check, "budget_scope": budget_scope,
              "whole_mission_budget_complete": False}
    output["_summary"] = {"task_key": output["task_key"], "task_type": output["task_type"],
                          "task_types": output["task_types"], "ordered": output["ordered"],
                          "assignments": [{k: row[k] for k in ("uav_id", "speed", "flight_alt", "jobs", "battery_level", "flight_remain",
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
