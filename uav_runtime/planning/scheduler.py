"""Optional isolated MILP adapter; importing this module never imports Pyomo."""

import json
import math
import subprocess
import sys

from uav_runtime.paths import ROOT

DEFAULT_WEIGHTS = {"makespan": 0.5, "total_range": 0.5, "total_wait": 0, "uav_count": 0}


class SchedulerError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def validate_scenario(data):
    def number(value, positive=False):
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or value < 0
            or (positive and value == 0)
        ):
            raise ValueError("costs and budgets must be finite nonnegative numbers")

    for key in ("M", "N"):
        if type(data.get(key)) is not int or not 1 <= data[key] <= 64:
            raise ValueError(key + " must be an integer from 1 to 64")
    m, n = data["M"], data["N"]
    if "job_groups" in data:
        groups = data["job_groups"]
        if (not isinstance(groups, dict) or set(groups) != {str(j) for j in range(1, n + 1)}
                or any(not isinstance(gid, str) or not gid.strip() for gid in groups.values())):
            raise ValueError("job_groups must map every job to a nonempty group ID")
    reserve_indices = data.get("new_reserve_indices", [])
    if (
        not isinstance(reserve_indices, list)
        or any(type(i) is not int or not 1 <= i <= m for i in reserve_indices)
        or len(reserve_indices) != len(set(reserve_indices))
    ):
        raise ValueError("invalid reserve candidate indices")
    reserve_maximum = data.get("max_new_reserves", m)
    if type(reserve_maximum) is not int or not 0 <= reserve_maximum <= m:
        raise ValueError("invalid reserve aircraft limit")
    maximum = data.get("max_active_uavs", m)
    if type(maximum) is not int or not 1 <= maximum <= m:
        raise ValueError("max_active_uavs must fit the candidate fleet")
    count = data.get("required_drones")
    if count is not None and (type(count) is not int or not (count == -1 or 1 <= count <= m)):
        raise ValueError(
            "required_drones must be null, -1, or an integer within the candidate fleet"
        )
    single = data.get("single_owner_jobs", [])
    if (
        not isinstance(single, list)
        or any(type(j) is not int or not 1 <= j <= n for j in single)
        or len(set(single)) != len(single)
    ):
        raise ValueError("invalid single-owner job list")
    if data.get("base_node", 0) != 0:
        raise ValueError("base_node must be zero")
    if (
        len(data["uavs"]) != m
        or any(type(u["id"]) is not int for u in data["uavs"])
        or [u["id"] for u in data["uavs"]] != list(range(1, m + 1))
    ):
        raise ValueError("UAV IDs must be ordered contiguous integers")
    number(data["epsilon"], True)
    number(data["big_m"], True)
    weights = data["lambda"]
    if (
        not isinstance(weights, dict)
        or not {"makespan", "total_range", "total_wait"} <= set(weights)
        or set(weights) - set(DEFAULT_WEIGHTS)
    ):
        raise ValueError("invalid objective weights")
    for key in weights:
        number(weights[key])
    if sum(weights.values()) <= 0:
        raise ValueError("at least one objective weight must be positive")
    for uav in data["uavs"]:
        number(uav["max_endurance"], True)
        if "max_range" in uav:
            number(uav["max_range"], True)
        for key in ("transfer_range", "transfer_time"):
            matrix = uav[key]
            if len(matrix) != n + 1 or any(len(row) != n + 1 for row in matrix):
                raise ValueError("transfer matrix shape mismatch")
            for j in range(n + 1):
                for k in range(n + 1):
                    if j != k:
                        number(matrix[j][k])
        for key in ("patrol_range_base", "patrol_time_base"):
            if len(uav[key]) != n + 1:
                raise ValueError("work vector shape mismatch")
            for value in uav[key][1:]:
                number(value, key == "patrol_time_base")
    eligibility = data.get("eligible_uavs", {})
    if not isinstance(eligibility, dict) or not isinstance(
        data.get("fixed_assignments", {}), dict
    ):
        raise ValueError("assignment constraints must be objects")
    for job, ids in eligibility.items():
        if (
            str(int(job)) != str(job)
            or not 1 <= int(job) <= n
            or not isinstance(ids, list)
            or not ids
            or any(type(uid) is not int or not 1 <= uid <= m for uid in ids)
            or len(ids) != len(set(ids))
        ):
            raise ValueError("invalid eligible UAV set")
    for job, uid in data.get("fixed_assignments", {}).items():
        if (
            str(int(job)) != str(job)
            or not 1 <= int(job) <= n
            or type(uid) is not int
            or not 1 <= uid <= m
        ):
            raise ValueError("invalid fixed assignment")
        if job in eligibility and uid not in eligibility[job]:
            raise ValueError("fixed UAV is not eligible for this job")


def verify_result(data, result):
    """Recompute route coverage and budgets from original costs, independently."""
    owners = {j: [] for j in range(1, data["N"] + 1)}
    if len(result["uavs"]) != data["M"]:
        raise ValueError("missing UAV result")
    for uid, row in enumerate(result["uavs"], 1):
        if type(row["id"]) is not int or row["id"] != uid:
            raise ValueError("UAV identity mismatch")
        nodes = row["route_nodes"]
        if (
            not isinstance(nodes, list)
            or not nodes
            or nodes[0] != 0
            or (len(nodes) > 1 and nodes[-1] != 0)
            or any(type(j) is not int or not 0 <= j <= data["N"] for j in nodes)
        ):
            raise ValueError("route does not return home or contains invalid nodes")
        jobs = nodes[1:-1]
        if 0 in jobs or len(set(jobs)) != len(jobs) or nodes == [0, 0]:
            raise ValueError("duplicate job or multiple home departures in one route")
        if "job_groups" in data and len({data["job_groups"][str(j)] for j in jobs}) > 1:
            raise ValueError("UAV route spans mutually exclusive job groups")
        for j in jobs:
            if uid not in data.get("eligible_uavs", {}).get(str(j), range(1, data["M"] + 1)):
                raise ValueError("ineligible job assignment")
            if data.get("fixed_assignments", {}).get(str(j), uid) != uid:
                raise ValueError("fixed assignment violation")
            owners[j].append(uid)
    if any(not who or len(who) > 2 for who in owners.values()):
        raise ValueError("incomplete job coverage or more than two owners")
    active_count = sum(len(row["route_nodes"]) > 1 for row in result["uavs"])
    new_reserves = sum(
        row["id"] in data.get("new_reserve_indices", []) and len(row["route_nodes"]) > 1
        for row in result["uavs"]
    )
    if new_reserves > data.get("max_new_reserves", data["M"]):
        raise ValueError("reserve expansion exceeds original fleet scale")
    if active_count > data.get("max_active_uavs", data["M"]):
        raise ValueError("maximum active UAV count violation")
    if (
        data.get("required_drones") not in (None, -1)
        and active_count != data["required_drones"]
    ):
        raise ValueError("required UAV count violation")
    for row in result["uavs"]:
        if "selected" in row and (
            type(row["selected"]) is not bool
            or row["selected"] != (len(row["route_nodes"]) > 1)
        ):
            raise ValueError("selected flag disagrees with actual departure")
    if "uav_count" in result and (
        type(result["uav_count"]) is not int or result["uav_count"] != active_count
    ):
        raise ValueError("reported UAV count disagrees with actual departures")
    if "zone_alloc" in result and result["zone_alloc"] != {
        str(j): len(who) for j, who in owners.items()
    }:
        raise ValueError("reported zone allocation disagrees with routes")
    if any(len(owners[j]) != 1 for j in data.get("single_owner_jobs", [])):
        raise ValueError("recovery job has multiple owners")
    regions = result.get("regions")
    if (
        not isinstance(regions, list)
        or len(regions) != data["N"]
        or any(type(r.get("id")) is not int for r in regions)
        or {r["id"] for r in regions} != set(owners)
    ):
        raise ValueError("invalid region coverage result")
    for region in regions:
        who = region.get("uavs")
        if (
            not isinstance(who, list)
            or any(type(uid) is not int for uid in who)
            or len(who) != len(set(who))
            or set(who) != set(owners[region["id"]])
        ):
            raise ValueError("region ownership disagrees with UAV routes")
    for uid, row in enumerate(result["uavs"], 1):
        cfg = data["uavs"][uid - 1]
        for key in ("endurance_used", "total_range"):
            if type(row[key]) not in (int, float) or not math.isfinite(row[key]):
                raise ValueError("nonfinite solver result")
        elapsed, distance = 0.0, 0.0
        for j, k in zip(row["route_nodes"], row["route_nodes"][1:]):
            if j:
                # Match upstream t2/s2: two owners each perform half the work.
                share = 1.0 / len(owners[j])
                elapsed += cfg["patrol_time_base"][j] * share
                distance += cfg["patrol_range_base"][j] * share
            elapsed += cfg["transfer_time"][j][k]
            distance += cfg["transfer_range"][j][k]
        # Synchronization wait remains part of endurance, never discarded.
        wait = row["endurance_used"] - elapsed
        if (
            not math.isfinite(wait)
            or wait < -0.02
            or row["endurance_used"] > cfg["max_endurance"] + 0.02
        ):
            raise ValueError("endurance accounting violation")
        if (
            abs(distance - row["total_range"]) > 0.02
            or distance > cfg.get("max_range", math.inf) + 0.02
        ):
            raise ValueError("range accounting violation")
    validation = {
        "passed": True,
        "single_owner": all(len(who) == 1 for who in owners.values()),
        "jobs": len(owners),
        "shared_jobs": sum(len(who) > 1 for who in owners.values()),
        "uav_count": active_count,
    }
    if "job_groups" in data:
        validation.update(group_exclusivity_checked=True,
                          job_groups=dict(data["job_groups"]),
                          groups=len(set(data["job_groups"].values())))
    return validation


def solve(data, python=None, time_limit=20):
    data = dict(data)
    data.setdefault("lambda", dict(DEFAULT_WEIGHTS))
    source_data = dict(data)
    if isinstance(data["lambda"], dict):
        data["lambda"] = dict(data["lambda"])
        data["lambda"].setdefault("uav_count", 0)
    if type(data.get("required_drones")) is int and data["required_drones"] == -1:
        data["required_drones"] = None
    validate_scenario(data)
    if (
        type(time_limit) not in (int, float)
        or not math.isfinite(time_limit)
        or not 0 < time_limit <= 120
    ):
        raise ValueError("time_limit must be in (0,120] seconds")
    command = [
        python or sys.executable,
        str(ROOT / "uav_runtime/solver/worker.py"),
    ]
    try:
        completed = subprocess.run(
            command,
            input=json.dumps({"scenario": data, "time_limit": time_limit}, allow_nan=False),
            text=True,
            capture_output=True,
            timeout=time_limit + 15,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SchedulerError(
            "SOLVER_TIMEOUT", "scheduler exceeded its process deadline"
        ) from exc
    except OSError as exc:
        raise SchedulerError(
            "SOLVER_UNAVAILABLE", "configured solver interpreter is unavailable"
        ) from exc
    try:
        result = json.loads(completed.stdout)
    except ValueError as exc:
        raise SchedulerError(
            "SOLVER_FAILED", "scheduler returned no valid JSON response"
        ) from exc
    if completed.returncode or result.get("error"):
        error = result.get("error") or {}
        raise SchedulerError(
            error.get("code", "SOLVER_FAILED"),
            error.get("message", "scheduler worker failed"),
        )
    if result.get("status") not in ("optimal", "feasible", "maxTimeLimit"):
        raise ValueError("scheduler returned no acceptable incumbent")
    from uav_runtime.mission_models import digest

    result["scenario_digest"] = digest(source_data)
    result["validation"] = verify_result(data, result)
    result["optimal"] = result["status"] == "optimal"
    result["objective_weights"] = dict(data["lambda"])
    return result
