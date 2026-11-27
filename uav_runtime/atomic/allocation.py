"""Assign supplied numerical jobs and visit orders through the verified solver."""
from copy import deepcopy
import time

from .common import AtomicError, finite, require, required
from uav_runtime.planning.scheduler import DEFAULT_WEIGHTS, SchedulerError, solve


def _records(rows, field, name):
    require(rows, name, list)
    if not 1 <= len(rows) <= 64:
        raise AtomicError("INVALID_ARGUMENT", f"{name} 数量须在 1 到 64 之间")
    ids = []
    for row in rows:
        require(row, f"{name}[]", dict)
        value = required(row, field)
        if not isinstance(value, str) or not value.strip() or value in ids:
            raise AtomicError("INVALID_ARGUMENT", f"{name}.{field} 须为唯一非空字符串")
        ids.append(value)
    return ids


def _nonnegative(value, name, positive=False):
    value = finite(value, name, positive=positive)
    if value < 0:
        raise AtomicError("INVALID_ARGUMENT", f"{name} 须非负")
    return value


def _vector(values, n, name, positive=False):
    require(values, name, list)
    if len(values) == n + 1:
        if isinstance(values[0], bool) or values[0] not in (None, 0):
            raise AtomicError("INVALID_ARGUMENT", f"{name} 的 base 项须为空或 0")
        values = values[1:]
    if len(values) != n:
        raise AtomicError("INVALID_ARGUMENT", f"{name} 必须按 jobs 顺序给出 {n} 项")
    return [None] + [_nonnegative(v, f"{name}[{i}]", positive) for i, v in enumerate(values)]


def _matrix_dimensions(values, job_ids, name, group_id=None):
    n = len(job_ids)
    require(values, name, list)
    if len(values) != n + 1 or any(not isinstance(row, list) or len(row) != n + 1 for row in values):
        details = {"field": name, "expected_jobs": list(job_ids), "expected_shape": [n + 1, n + 1],
                   "actual_shape": {"rows": len(values),
                                    "row_lengths": [len(row) if isinstance(row, list) else None for row in values]}}
        if group_id is not None:
            details["group_id"] = group_id
        raise AtomicError("INVALID_ARGUMENT", f"{name} 必须是 {n+1}×{n+1} 矩阵", **details)


def _matrix(values, job_ids, reachable, name):
    _matrix_dimensions(values, job_ids, name)
    result = []
    for j, row in enumerate(values):
        out = []
        for k, value in enumerate(row):
            if j == k:
                if isinstance(value, bool) or value not in (None, 0):
                    raise AtomicError("INVALID_ARGUMENT", f"{name} 对角线须为空或 0")
                out.append(None)
                continue
            if value is None:
                unusable = ((j > 0 and job_ids[j-1] not in reachable)
                            or (k > 0 and job_ids[k-1] not in reachable))
                if not unusable:
                    raise AtomicError("MISSING_COST", f"{name}[{j}][{k}] 缺少可执行航段成本")
                # The eligibility constraints prevent these arcs being selected.
                out.append(0.0)
            else:
                out.append(_nonnegative(value, f"{name}[{j}][{k}]"))
        result.append(out)
    return result


def _grouped_costs(data, candidates, uids):
    """Validate independent numerical blocks and map them into one MILP input."""
    groups = require(data["groups"], "groups", list)
    _records(groups, "group_id", "groups")
    candidate_by_id = {row["uav_id"]: row for row in candidates}
    jobs, blocks = [], []
    for group in groups:
        if set(group) - {"group_id", "jobs", "costs", "candidates"}:
            raise AtomicError("INVALID_ARGUMENT", "group 含不支持的字段")
        gid = group["group_id"]
        block_jobs = required(group, "jobs")
        jids = _records(block_jobs, "job_id", f"groups.{gid}.jobs")
        if len(jobs) + len(block_jobs) > 64:
            raise AtomicError("INVALID_ARGUMENT", "全部组的作业总数须不超过 64")
        if any(jid in {row["job_id"] for row in jobs} for jid in jids):
            raise AtomicError("INVALID_ARGUMENT", "跨组 job_id 须全局唯一", group_id=gid)
        costs = require(required(group, "costs"), f"groups.{gid}.costs", dict)
        maps = {key: require(required(costs, key), key, dict)
                for key in ("transfer_range", "transfer_time", "reachable")}
        block_uids = set(maps["reachable"])
        if not block_uids:
            raise AtomicError("NO_FEASIBLE_ALLOCATION", "任务组没有数值成本候选机", group_id=gid)
        if any(set(values) != block_uids for values in maps.values()) or not block_uids <= set(uids):
            raise AtomicError("INVALID_ARGUMENT", "组内成本键须一致且为全局候选机子集", group_id=gid)
        # Diagnose a whole-problem block used for one group before the optional
        # base-vector convention can obscure that actual dimension mismatch.
        for key in ("transfer_range", "transfer_time"):
            for uid in uids:
                if uid in block_uids:
                    _matrix_dimensions(maps[key][uid], jids, f"{key}.{uid}", gid)
        if "candidates" in group:
            supplied = required(group, "candidates")
            if set(_records(supplied, "uav_id", f"groups.{gid}.candidates")) != block_uids:
                raise AtomicError("INVALID_ARGUMENT", "组内候选编号须与成本键一致", group_id=gid)
            for row in supplied:
                for key in ("range_budget", "time_budget"):
                    actual = finite(required(row, key), key, True)
                    expected = finite(required(candidate_by_id[row["uav_id"]], key), key, True)
                    if actual != expected:
                        raise AtomicError("BUDGET_MISMATCH", "同机的组内预算与全局预算不一致",
                                          group_id=gid, uav_id=row["uav_id"], field=key,
                                          group_value=actual, global_value=expected)
        clean = {"work_range": _vector(required(costs, "work_range"), len(jids), "work_range"),
                 "work_time": _vector(required(costs, "work_time"), len(jids), "work_time", True),
                 "work_range_by_uav": {}, "work_time_by_uav": {},
                 "transfer_range": {}, "transfer_time": {}, "reachable": {}}
        raw_wr_uav = costs.get("work_range_by_uav")
        raw_wt_uav = costs.get("work_time_by_uav")
        for uid in block_uids:
            reachable = require(maps["reachable"][uid], f"{gid}.reachable.{uid}", list)
            if (any(not isinstance(jid, str) or jid not in jids for jid in reachable)
                    or len(reachable) != len(set(reachable))):
                raise AtomicError("INVALID_ARGUMENT", "组内 reachable 含未知或重复作业", group_id=gid, uav_id=uid)
            clean["reachable"][uid] = list(reachable)
            for key in ("transfer_range", "transfer_time"):
                clean[key][uid] = _matrix(maps[key][uid], jids, reachable, f"{gid}.{key}.{uid}")
            if isinstance(raw_wr_uav, dict) and uid in raw_wr_uav:
                clean["work_range_by_uav"][uid] = _vector(raw_wr_uav[uid], len(jids), f"{gid}.work_range_by_uav.{uid}")
            if isinstance(raw_wt_uav, dict) and uid in raw_wt_uav:
                clean["work_time_by_uav"][uid] = _vector(raw_wt_uav[uid], len(jids), f"{gid}.work_time_by_uav.{uid}", True)
        offset = len(jobs)
        jobs.extend(deepcopy(block_jobs))
        blocks.append({"group_id": gid, "job_ids": jids, "candidate_uav_ids": [uid for uid in uids if uid in block_uids],
                       "offset": offset, "costs": clean})
    n = len(jobs)
    # Missing candidate/group pairs are ineligible. Cross-group cells are only
    # internal placeholders: the model fixes those arcs to zero, never flies them.
    merged = {key: {uid: [[None if j == k else 0.0 for k in range(n + 1)] for j in range(n + 1)]
                    for uid in uids} for key in ("transfer_range", "transfer_time")}
    has_per_uav_work = any(block["costs"]["work_range_by_uav"] for block in blocks)
    merged.update(work_range=[], work_time=[], reachable={uid: [] for uid in uids})
    if has_per_uav_work:
        merged["work_range_by_uav"] = {uid: [] for uid in uids}
        merged["work_time_by_uav"] = {uid: [] for uid in uids}
    for block in blocks:
        clean = block["costs"]
        positions = [0] + [block["offset"] + j + 1 for j in range(len(block["job_ids"]))]
        for key in ("work_range", "work_time"):
            merged[key].extend(clean[key][1:])
        if has_per_uav_work:
            for uid in uids:
                u_wr = clean["work_range_by_uav"].get(uid, clean["work_range"])
                u_wt = clean["work_time_by_uav"].get(uid, clean["work_time"])
                merged["work_range_by_uav"][uid].extend(u_wr[1:])
                merged["work_time_by_uav"][uid].extend(u_wt[1:])
        for uid in block["candidate_uav_ids"]:
            merged["reachable"][uid].extend(clean["reachable"][uid])
            for key in ("transfer_range", "transfer_time"):
                for j, target_j in enumerate(positions):
                    for k, target_k in enumerate(positions):
                        merged[key][uid][target_j][target_k] = clean[key][uid][j][k]
    context = [{key: value for key, value in block.items() if key != "costs"} for block in blocks]
    return jobs, merged, context


def _infeasibility_diagnosis(scenario, uids, jids):
    """Prove a waiting lower bound only for forced pairs with a sole job."""
    if scenario.get("required_drones") != len(uids):
        return {}
    eligible = scenario["eligible_uavs"]
    violations = []
    for job, pair in eligible.items():
        j = int(job)
        if (len(pair) != 2 or j in scenario["single_owner_jobs"]
                or job in scenario["fixed_assignments"]):
            continue
        # shortcut: no multi-job route bounds; extend only with a proven bound.
        if any(sum(i in ids for ids in eligible.values()) != 1 for i in pair):
            continue
        for i, other in (pair, pair[::-1]):
            row, peer = scenario["uavs"][i - 1], scenario["uavs"][other - 1]
            arrival = row["transfer_time"][0][j]
            peer_arrival = peer["transfer_time"][0][j]
            work = row["patrol_time_base"][j] / 2
            wait = max(0.0, peer_arrival + scenario["epsilon"] - work - arrival)
            minimum = arrival + wait + work + row["transfer_time"][j][0]
            budget = row["max_endurance"]
            if minimum > budget + 1e-6:
                violations.append({"job_id": jids[j - 1], "uav_id": uids[i - 1],
                                   "peer_uav_id": uids[other - 1],
                                   "arrival_lower_bound_s": arrival,
                                   "peer_arrival_lower_bound_s": peer_arrival,
                                   "shared_work_time_s": work, "waiting_lower_bound_s": wait,
                                   "return_time_s": row["transfer_time"][j][0],
                                   "required_time_lower_bound_s": minimum,
                                   "time_budget_s": budget, "excess_s": minimum - budget})
    if not violations:
        return {}
    return {"binding_constraint": "SHARED_JOB_TIME_BUDGET",
            "scope": "forced_pair_single_job_necessary_bound",
            "message": "指定出动数量使这两架机必须共担唯一作业；满足作业时段重叠的等待与返航时间下界超过预算。",
            "violations": violations}


def compute(data):
    """仅根据数值成本与约束求解无人机任务归属和访问顺序。"""
    require(data, "input", dict)
    if set(data) - {"candidates", "jobs", "costs", "groups", "constraints", "extra_constraints", "solver_options"}:
        raise AtomicError("INVALID_ARGUMENT", "分配输入含不支持的字段")
    candidates = required(data, "candidates")
    uids = _records(candidates, "uav_id", "candidates")
    groups = None
    if "groups" in data:
        if "jobs" in data or "costs" in data:
            raise AtomicError("INVALID_ARGUMENT", "groups 与顶层 jobs/costs 是互斥输入形式")
        jobs, costs, groups = _grouped_costs(data, candidates, uids)
    else:
        jobs = required(data, "jobs")
        costs = require(required(data, "costs"), "costs", dict)
    jids = _records(jobs, "job_id", "jobs")
    m, n = len(uids), len(jids)
    reach = require(required(costs, "reachable"), "costs.reachable", dict)
    distances = require(required(costs, "transfer_range"), "transfer_range", dict)
    times = require(required(costs, "transfer_time"), "transfer_time", dict)
    for name, values in (("reachable", reach), ("transfer_range", distances), ("transfer_time", times)):
        if set(values) != set(uids):
            raise AtomicError("INVALID_ARGUMENT", f"{name} 的无人机编号必须与 candidates 完全一致")
    work_range = _vector(required(costs, "work_range"), n, "work_range")
    work_time = _vector(required(costs, "work_time"), n, "work_time", positive=True)
    work_range_by_uav = costs.get("work_range_by_uav")
    work_time_by_uav = costs.get("work_time_by_uav")
    if work_range_by_uav is not None:
        require(work_range_by_uav, "costs.work_range_by_uav", dict)
    if work_time_by_uav is not None:
        require(work_time_by_uav, "costs.work_time_by_uav", dict)
    eligible = {jid: [] for jid in jids}
    rows = []
    for index, candidate in enumerate(candidates, 1):
        uid = candidate["uav_id"]
        reachable = require(reach[uid], f"reachable.{uid}", list)
        if (any(not isinstance(jid, str) or jid not in jids for jid in reachable)
                or len(reachable) != len(set(reachable))):
            raise AtomicError("INVALID_ARGUMENT", f"reachable.{uid} 包含未知或重复作业")
        for jid in reachable:
            eligible[jid].append(index)
        uav_work_range = (
            _vector(work_range_by_uav[uid], n, f"work_range_by_uav.{uid}")
            if work_range_by_uav is not None and uid in work_range_by_uav
            else list(work_range)
        )
        speed = candidate.get("planning_speed")
        if work_time_by_uav is not None and uid in work_time_by_uav:
            base_time = _vector(work_time_by_uav[uid], n, f"work_time_by_uav.{uid}", positive=True)
        elif speed is not None:
            speed = finite(speed, f"{uid}.planning_speed", True)
            base_time = [None] + [w / speed for w in uav_work_range[1:]]
        else:
            base_time = list(work_time)
        rows.append({"id": index,
                     "max_range": finite(required(candidate, "range_budget"), f"{uid}.range_budget", True),
                     "max_endurance": finite(required(candidate, "time_budget"), f"{uid}.time_budget", True),
                     "transfer_range": _matrix(distances[uid], jids, reachable, f"transfer_range.{uid}"),
                     "transfer_time": _matrix(times[uid], jids, reachable, f"transfer_time.{uid}"),
                     "patrol_range_base": uav_work_range, "patrol_time_base": base_time})
    constraints = deepcopy(require(data.get("constraints", {}), "constraints", dict))
    if set(constraints) - {"fixed_assignments", "required_drones", "single_owner_jobs", "allowed_assignments"}:
        raise AtomicError("INVALID_ARGUMENT", "constraints 含不支持的字段")
    if "extra_constraints" in data and data["extra_constraints"] is not None:
        extra = require(data["extra_constraints"], "extra_constraints", dict)
        if set(extra) - {"fixed_assignments", "required_drones", "single_owner_jobs", "allowed_assignments"}:
            raise AtomicError("INVALID_ARGUMENT", "extra_constraints 含不支持的字段")
        if "fixed_assignments" in extra:
            extra_fixed = require(extra["fixed_assignments"], "extra_constraints.fixed_assignments", dict)
            base_fixed = require(constraints.setdefault("fixed_assignments", {}), "fixed_assignments", dict)
            for jid, uid in extra_fixed.items():
                if jid in base_fixed and base_fixed[jid] != uid:
                    raise AtomicError("INVALID_ARGUMENT", "extra_constraints 与已有固定归属冲突", job_id=jid)
                base_fixed[jid] = uid
        if "single_owner_jobs" in extra:
            extra_single = require(extra["single_owner_jobs"], "extra_constraints.single_owner_jobs", list)
            base_single = require(constraints.setdefault("single_owner_jobs", []), "single_owner_jobs", list)
            for jid in extra_single:
                if jid not in base_single:
                    base_single.append(jid)
        if "allowed_assignments" in extra:
            extra_allowed = require(extra["allowed_assignments"], "extra_constraints.allowed_assignments", dict)
            base_allowed = require(constraints.setdefault("allowed_assignments", {}), "allowed_assignments", dict)
            for jid, who in extra_allowed.items():
                require(who, f"extra_constraints.allowed_assignments.{jid}", list)
                if jid in base_allowed:
                    require(base_allowed[jid], f"allowed_assignments.{jid}", list)
                    base_allowed[jid] = [u for u in base_allowed[jid] if u in who]
                else:
                    base_allowed[jid] = list(who)
        if "required_drones" in extra:
            if "required_drones" in constraints and constraints["required_drones"] != extra["required_drones"]:
                raise AtomicError("INVALID_ARGUMENT", "extra_constraints.required_drones 与已有约束冲突")
            constraints["required_drones"] = extra["required_drones"]
    allowed = require(constraints.get("allowed_assignments", {}), "allowed_assignments", dict)
    for jid, allowed_uids in allowed.items():
        if not isinstance(jid, str) or jid not in jids:
            raise AtomicError("INVALID_ARGUMENT", "allowed_assignments 引用未知作业")
        require(allowed_uids, f"allowed_assignments.{jid}", list)
        if (any(not isinstance(uid, str) or uid not in uids for uid in allowed_uids)
                or len(allowed_uids) != len(set(allowed_uids))):
            raise AtomicError("INVALID_ARGUMENT", "allowed_assignments 含未知或重复候选机", job_id=jid)
        # Physical costs were validated above; narrow eligibility without
        # treating a user exclusion as permission to omit reachable-arc costs.
        eligible[jid] = [index for index in eligible[jid] if uids[index-1] in allowed_uids]
    missing = [jid for jid, ids in eligible.items() if not ids]
    if missing:
        raise AtomicError("NO_FEASIBLE_ALLOCATION", "作业没有可执行候选机", jobs=missing)
    fixed = require(constraints.get("fixed_assignments", {}), "fixed_assignments", dict)
    fixed_indices = {}
    for jid, uid in fixed.items():
        if jid not in jids or not isinstance(uid, str) or uid not in uids:
            raise AtomicError("INVALID_ARGUMENT", "固定归属引用未知作业或候选机")
        ui = uids.index(uid) + 1
        if ui not in eligible[jid]:
            raise AtomicError("NO_FEASIBLE_ALLOCATION", "指定执行机无法执行该作业", job_id=jid, uav_id=uid)
        fixed_indices[str(jids.index(jid) + 1)] = ui
    single = require(constraints.get("single_owner_jobs", []), "single_owner_jobs", list)
    if any(not isinstance(jid, str) or jid not in jids for jid in single) or len(single) != len(set(single)):
        raise AtomicError("INVALID_ARGUMENT", "single_owner_jobs 包含未知或重复作业")
    required_count = constraints.get("required_drones")
    if required_count is not None:
        if type(required_count) is not int or required_count < 1:
            raise AtomicError("INVALID_ARGUMENT", "required_drones 须为正整数")
        if required_count > m:
            raise AtomicError("NO_FEASIBLE_ALLOCATION", "候选机数量少于要求的实际出动架数")
    options = require(data.get("solver_options", {}), "solver_options", dict)
    if set(options) - {"time_limit_sec", "weights"}:
        raise AtomicError("INVALID_ARGUMENT", "solver_options 含不支持的字段")
    time_limit = finite(options.get("time_limit_sec", 20), "time_limit_sec", True)
    if time_limit > 120:
        raise AtomicError("INVALID_ARGUMENT", "time_limit_sec 须不超过 120")
    weights = require(options.get("weights", dict(DEFAULT_WEIGHTS)), "weights", dict)
    if set(weights) - set(DEFAULT_WEIGHTS):
        raise AtomicError("INVALID_ARGUMENT", "weights 含未知目标")
    weights = {key: _nonnegative(weights.get(key, 0), f"weights.{key}") for key in DEFAULT_WEIGHTS}
    if sum(weights.values()) <= 0:
        raise AtomicError("INVALID_ARGUMENT", "至少一个优化目标的权重须大于 0")
    scenario = {"M": m, "N": n, "base_node": 0, "epsilon": 0.001,
                "big_m": max(row["max_endurance"] for row in rows), "uavs": rows,
                "lambda": weights, "fixed_assignments": fixed_indices,
                "eligible_uavs": {str(i + 1): eligible[jid] for i, jid in enumerate(jids)},
                "single_owner_jobs": [jids.index(jid) + 1 for jid in single]}
    if required_count is not None:
        scenario["required_drones"] = required_count
    if groups is not None:
        scenario["job_groups"] = {str(jids.index(jid) + 1): group["group_id"]
                                  for group in groups for jid in group["job_ids"]}
    started = time.monotonic()
    try:
        result = solve(scenario, time_limit=time_limit)
    except SchedulerError as exc:
        code = "NO_FEASIBLE_ALLOCATION" if exc.code == "PLANNING_INFEASIBLE" else exc.code
        details = {}
        if exc.code == "PLANNING_INFEASIBLE":
            details["diagnosis"] = _infeasibility_diagnosis(scenario, uids, jids) or {
                "binding_constraint": "UNDETERMINED",
                "message": "给定数值成本与约束下无可行分配，尚无可证明的具体绑定约束。"}
        raise AtomicError(code, str(exc), **details) from exc
    except ValueError as exc:
        raise AtomicError("SOLVER_FAILED", str(exc)) from exc
    assignments = [{"uav_id": uids[row["id"]-1],
                    "job_ids": [jids[j-1] for j in row["route_nodes"] if j],
                    "estimated_range_m": row["total_range"]}
                   for row in result["uavs"] if len(row["route_nodes"]) > 1]
    owners = {jids[row["id"]-1]: [uids[i-1] for i in row["uavs"]] for row in result["regions"]}
    used = {row["uav_id"] for row in assignments}
    solver = {"status": result["status"], "optimal": result["optimal"],
              "seconds": round(time.monotonic()-started, 3), "validation": result["validation"],
              "timing_note": "原始求解器等待变量可能含松弛量，endurance_used/finish_time 不作为预计耗时；本结果仅证明给定数值成本与预算下的分配可行。"}
    grouped_output = {}
    if groups is not None:
        job_groups = {jid: group["group_id"] for group in groups for jid in group["job_ids"]}
        for assignment in assignments:
            assignment["group_id"] = job_groups[assignment["job_ids"][0]]
        by_group = {}
        for group in groups:
            gid = group["group_id"]
            rows_for_group = [deepcopy(row) for row in assignments if row["group_id"] == gid]
            used_in_group = {row["uav_id"] for row in rows_for_group}
            by_group[gid] = {"status": "OK", "group_id": gid, "assignments": rows_for_group,
                             "owners": {jid: deepcopy(owners[jid]) for jid in group["job_ids"]},
                             "unused": [uid for uid in group["candidate_uav_ids"] if uid not in used_in_group],
                             "index": {"uav_ids": group["candidate_uav_ids"], "job_ids": group["job_ids"], "scope": "group_view"},
                             "solver": {**deepcopy(solver), "scope": "global_joint_solve"},
                             "solver_evidence": {"scope": "global_joint_solve", "result_pointer": "/data/solver_result",
                                                 "scenario_digest": result["scenario_digest"],
                                                 "validation": deepcopy(result["validation"]),
                                                 "global_uav_indices": [uids.index(uid) + 1 for uid in group["candidate_uav_ids"]],
                                                 "global_job_indices": [jids.index(jid) + 1 for jid in group["job_ids"]]},
                             "estimated_only": True, "sharing_model": "equal_work_estimate"}
        grouped_output = {"allocations_by_group": by_group, "job_groups": job_groups,
                          "group_exclusivity": "each_uav_at_most_one_group"}
    summary = {"assignments": assignments, "owners": owners,
               "unused": [uid for uid in uids if uid not in used], "solver": solver,
               "estimated_only": True}
    if groups is not None:
        summary["groups"] = [{"group_id": gid, "assignments": value["assignments"]} for gid, value in by_group.items()]
    return {"status": "OK", **summary, **grouped_output, "index": {"uav_ids": uids, "job_ids": jids},
            "sharing_model": "equal_work_estimate", "constraints": deepcopy(constraints),
            "solver_input": scenario, "solver_result": result, "_summary": summary}
