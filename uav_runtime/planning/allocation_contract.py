"""Check supplied ownership against numerical solver records; no solving or I/O."""
import hashlib
import json
import math


class AllocationEvidenceError(ValueError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


def _fail(message, code="INVALID_ALLOCATION_EVIDENCE", **details):
    raise AllocationEvidenceError(code, message, **details)


def _object(value, name):
    if not isinstance(value, dict):
        _fail(f"{name} 须为对象")
    return value


def _ids(value, name, nonempty=True):
    if (not isinstance(value, list) or (nonempty and not value)
            or any(not isinstance(x, str) or not x.strip() for x in value)
            or len(set(value)) != len(value)):
        _fail(f"{name} 须为不重复的非空字符串数组")
    return value


def _indices(value, size, name, nonempty=True):
    if (not isinstance(value, list) or (nonempty and not value)
            or any(type(x) is not int or not 1 <= x <= size for x in value)
            or len(set(value)) != len(value)):
        _fail(f"{name} 数字索引缺失、重复或越界")
    return value


def _digest(value):
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_decision(assignments, owners=None):
    """Ignore presentation order/metrics, but retain each aircraft's visit order."""
    if not isinstance(assignments, list) or not assignments:
        _fail("assignments 须为非空数组")
    routes, derived = {}, {}
    for row in assignments:
        _object(row, "assignments[]")
        uid = row.get("uav_id")
        if not isinstance(uid, str) or not uid.strip() or uid in routes:
            _fail("assignments.uav_id 缺失或重复")
        jobs = _ids(row.get("job_ids"), "assignments.job_ids")
        routes[uid] = list(jobs)
        for jid in jobs:
            derived.setdefault(jid, []).append(uid)
    if any(len(uids) > 2 for uids in derived.values()):
        _fail("每个作业最多两架机共担")
    derived = {jid: sorted(uids) for jid, uids in sorted(derived.items())}
    if owners is not None:
        _object(owners, "owners")
        if any(not isinstance(jid, str) or not jid.strip() for jid in owners):
            _fail("owners 作业编号须为非空字符串")
        supplied = {jid: sorted(_ids(uids, "owners[]")) for jid, uids in owners.items()}
        if supplied != derived:
            _fail("owners 与访问序列的作业归属不一致", "ALLOCATION_EVIDENCE_MISMATCH")
    return {"assignments": [{"uav_id": uid, "job_ids": jobs} for uid, jobs in sorted(routes.items())],
            "owners": derived}


def _same(actual, expected, message, **details):
    if actual != expected:
        _fail(message, "ALLOCATION_EVIDENCE_MISMATCH", **details)


def _index(allocation):
    index = _object(allocation.get("index"), "allocation.index")
    return (_ids(index.get("uav_ids"), "index.uav_ids"),
            _ids(index.get("job_ids"), "index.job_ids"))


def _unused(allocation, uids, decision):
    if "unused" in allocation:
        supplied = _ids(allocation["unused"], "unused", nonempty=False)
        used = {row["uav_id"] for row in decision["assignments"]}
        _same(set(supplied), set(uids) - used, "unused 与当前范围的参与集合不一致")


def _full_record(allocation):
    """Rebuild from raw routes AND region owners, never from checked labels."""
    index = allocation.get("index")
    if not isinstance(index, dict) or not {"uav_ids", "job_ids"} <= set(index):
        _fail("完整求解证据需要机号与作业索引", "MISSING_ALLOCATION_EVIDENCE")
    uids, jids = _index(allocation)
    if "owners" not in allocation:
        _fail("完整分配记录缺少 owners", "MISSING_ALLOCATION_EVIDENCE")
    result = allocation.get("solver_result")
    if not isinstance(result, dict) or "regions" not in result or "uavs" not in result:
        _fail("需要完整 index 和原始 solver_result", "MISSING_ALLOCATION_EVIDENCE")
    if _object(result.get("validation"), "solver_result.validation").get("passed") is not True:
        _fail("原始求解记录没有通过结果校验")
    if not isinstance(result.get("scenario_digest"), str) or not result["scenario_digest"]:
        _fail("原始求解记录缺少 scenario_digest", "MISSING_ALLOCATION_EVIDENCE")
    if allocation.get("status", "OK") != "OK":
        _fail("分配产物状态不是 OK")
    if "solver" in allocation:
        solver = _object(allocation["solver"], "solver")
        if _object(solver.get("validation"), "solver.validation").get("passed") is not True:
            _fail("分配结果没有通过结果校验")
    raw_rows = result["uavs"]
    if not isinstance(raw_rows, list):
        _fail("solver_result.uavs 须为数组")
    found, assignments = set(), []
    for row in raw_rows:
        _object(row, "solver_result.uavs[]")
        number = row.get("id")
        if type(number) is not int or not 1 <= number <= len(uids) or number in found:
            _fail("求解记录的机号索引重复或越界")
        found.add(number)
        route = row.get("route_nodes")
        if not isinstance(route, list) or not route:
            _fail("原始求解记录缺少完整 route_nodes", "MISSING_ALLOCATION_EVIDENCE")
        if any(type(n) is not int for n in route):
            _fail("route_nodes 须为整数索引，布尔值不是有效索引")
        if route == [0]:
            if row.get("selected") is not False:
                _fail("无作业路线的 selected 须为 false")
            continue
        if len(route) < 3 or route[0] != 0 or route[-1] != 0:
            _fail("参与机路线须以基地 0 起止且包含作业")
        nodes = _indices(route[1:-1], len(jids), "route_nodes 作业段")
        if row.get("selected") is not True:
            _fail("有作业路线的 selected 须为 true")
        assignments.append({"uav_id": uids[number - 1], "job_ids": [jids[n - 1] for n in nodes]})
    if found != set(range(1, len(uids) + 1)):
        _fail("原始求解记录未覆盖全部候选索引")
    regions = result["regions"]
    if not isinstance(regions, list):
        _fail("solver_result.regions 须为数组")
    owners, region_numbers = {}, set()
    for row in regions:
        _object(row, "solver_result.regions[]")
        number = row.get("id")
        if type(number) is not int or not 1 <= number <= len(jids) or number in region_numbers:
            _fail("求解记录的作业索引重复或越界")
        region_numbers.add(number)
        participants = _indices(row.get("uavs"), len(uids), "solver_result.regions.uavs")
        if len(participants) > 2:
            _fail("原始求解记录的作业共担数量超限")
        owners[jids[number - 1]] = [uids[n - 1] for n in participants]
    if region_numbers != set(range(1, len(jids) + 1)):
        _fail("原始求解记录未覆盖全部作业索引")
    rebuilt = normalize_decision(assignments, owners)
    supplied = normalize_decision(allocation.get("assignments"), allocation.get("owners"))
    _same(supplied, rebuilt, "表层分配与原始求解归属或访问顺序不同")
    _unused(allocation, uids, rebuilt)
    if "uav_count" in result:
        _same(result["uav_count"], len(assignments), "原始求解参与数量与路线不同")
    if "job_groups" in allocation:
        groups = _object(allocation["job_groups"], "job_groups")
        if set(groups) != set(jids) or any(not isinstance(g, str) or not g.strip() for g in groups.values()):
            _fail("job_groups 须覆盖全部作业且组号非空")
        for row in rebuilt["assignments"]:
            group_ids = {groups[jid] for jid in row["job_ids"]}
            if len(group_ids) != 1:
                _fail("全局求解路线跨越独立任务组", "ALLOCATION_EVIDENCE_MISMATCH", uav_id=row["uav_id"])
        for row in allocation["assignments"]:
            if "group_id" in row:
                _same(row["group_id"], groups[row["job_ids"][0]], "分配项 group_id 与作业分组不同")
    return rebuilt, result


def _group_record(view, global_allocation):
    if global_allocation is None:
        _fail("分组视图需要 global_allocation 的完整全局求解记录", "MISSING_ALLOCATION_EVIDENCE")
    global_allocation = _object(global_allocation, "global_allocation")
    decision, result = _full_record(global_allocation)
    guids, gjids = _index(global_allocation)
    uids, jids = _index(view)
    if view.get("status", "OK") != "OK":
        _fail("分组视图状态不是 OK")
    _same(view["index"].get("scope"), "group_view", "分组索引 scope 非法")
    if "solver" in view:
        solver = _object(view["solver"], "solver")
        _same(solver.get("scope"), "global_joint_solve", "分组求解 scope 非法")
        if _object(solver.get("validation"), "solver.validation").get("passed") is not True:
            _fail("分组视图没有通过结果校验")
    gid = view.get("group_id")
    groups = _object(global_allocation.get("job_groups"), "global_allocation.job_groups")
    if not isinstance(gid, str) or not gid or gid not in groups.values():
        _fail("分组视图 group_id 不在全局证据中")
    _same(set(jids), {jid for jid, group in groups.items() if group == gid}, "分组作业范围与全局记录不同")
    evidence = _object(view.get("solver_evidence"), "solver_evidence")
    _same(evidence.get("scope"), "global_joint_solve", "分组证据 scope 非法")
    _same(evidence.get("result_pointer"), "/data/solver_result", "分组证据指针不是全局原始结果")
    scenario = result.get("scenario_digest")
    if not isinstance(scenario, str) or not scenario:
        _fail("全局证据缺少 scenario_digest", "MISSING_ALLOCATION_EVIDENCE")
    _same(evidence.get("scenario_digest"), scenario, "分组与全局 scenario_digest 不一致")
    if _object(evidence.get("validation"), "solver_evidence.validation").get("passed") is not True:
        _fail("分组证据没有通过结果校验")
    for local, global_ids, key in ((uids, guids, "global_uav_indices"), (jids, gjids, "global_job_indices")):
        indices = _indices(evidence.get(key), len(global_ids), key)
        _same([global_ids[i - 1] for i in indices], local, f"{key} 未逐项映射到同名 ID")
    projected = normalize_decision([row for row in decision["assignments"] if row["job_ids"][0] in jids])
    if any(row["uav_id"] not in uids for row in projected["assignments"]):
        _fail("分组候选索引缺少全局实际参与机")
    _same(normalize_decision(view.get("assignments"), view.get("owners")), projected,
          "分组视图与全局求解归属或顺序不同")
    _unused(view, uids, projected)
    for row in view["assignments"]:
        if "group_id" in row:
            _same(row["group_id"], gid, "分组分配项 group_id 不一致")
    return projected, result


def _wait_only(allocation, decision):
    """Legacy numeric wait records have no route proof; validate only their shape."""
    result = _object(allocation.get("solver_result"), "solver_result")
    if _object(result.get("validation"), "solver_result.validation").get("passed") is not True:
        _fail("等待证据没有通过结果校验")
    index = _object(allocation.get("index"), "allocation.index")
    uids = _ids(index.get("uav_ids"), "index.uav_ids")
    rows = result.get("uavs")
    if not isinstance(rows, list):
        _fail("等待证据缺少 uavs", "MISSING_ALLOCATION_EVIDENCE")
    found = {}
    for row in rows:
        _object(row, "solver_result.uavs[]")
        number = row.get("id")
        if type(number) is not int or not 1 <= number <= len(uids) or number in found:
            _fail("等待证据的机号索引重复或越界")
        found[number] = row
    for assigned in decision["assignments"]:
        uid = assigned["uav_id"]
        if uid not in uids or uids.index(uid) + 1 not in found:
            _fail("等待证据缺少参与机", "MISSING_ALLOCATION_EVIDENCE", uav_id=uid)
        segments = found[uids.index(uid) + 1].get("segments")
        if not isinstance(segments, list) or not segments:
            _fail("等待证据缺少 segments", "MISSING_ALLOCATION_EVIDENCE", uav_id=uid)
        for segment in segments:
            value = _object(segment, "segments[]").get("wait_time")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                _fail("等待证据的 wait_time 须为非负有限数")


def verify_allocation(assignments, allocation=None, global_allocation=None, *, allow_wait_only=False):
    """Return a fresh scope declaration; never trust input provenance/checked tags."""
    effective = normalize_decision(assignments)
    check = {"basis": "supplied_values", "scope": "ownership_and_visit_order",
             "decision_digest": _digest(effective), "solver_result_checked": False,
             "producer_authenticated": False}
    if allocation is None:
        if global_allocation is not None:
            _fail("global_allocation 只能与分组 allocation 一起提供")
        return check
    allocation = _object(allocation, "allocation")
    if "owners" not in allocation:
        _fail("allocation 缺少 owners")
    _same(normalize_decision(allocation.get("assignments"), allocation["owners"]), effective,
          "实际输入与 allocation 的归属或访问顺序不同")
    index = allocation.get("index", {})
    solver = allocation.get("solver", {})
    grouped = ("solver_evidence" in allocation or (isinstance(index, dict) and index.get("scope") == "group_view")
               or (isinstance(solver, dict) and solver.get("scope") == "global_joint_solve"))
    if grouped:
        rebuilt, result = _group_record(allocation, global_allocation)
        check["group_id"] = allocation.get("group_id")
    else:
        if global_allocation is not None:
            _fail("非分组输入不能附带 global_allocation")
        result = allocation.get("solver_result")
        claims_solver = "solver" in allocation or "solver_result" in allocation
        if not claims_solver:
            return check
        if not isinstance(result, dict):
            _fail("需要原始 solver_result", "MISSING_ALLOCATION_EVIDENCE")
        rows = result.get("uavs", [])
        has_routes = "regions" in result or (isinstance(rows, list) and any(isinstance(r, dict) and "route_nodes" in r for r in rows))
        if not has_routes and allow_wait_only:
            _wait_only(allocation, effective)
            check["basis"] = "wait_evidence_only"
            return check
        rebuilt, result = _full_record(allocation)
    _same(effective, rebuilt, "实际输入与原始求解归属或访问顺序不同")
    check.update(basis="solver_result_matched", solver_result_checked=True,
                 solver_scenario_digest=result.get("scenario_digest"))
    return check
