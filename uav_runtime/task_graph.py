"""Validated, bounded mission graphs. No dispatch, database writes or flight control."""
from copy import deepcopy
import re

from .atomic.common import AtomicError, finite, require, required
from .budget_math import DEFAULT_REF_ALT_M

GEOMETRY_ACTIONS = {"search", "patrol"}
INTERFACE_ACTIONS = {"record_start", "record_stop", "photo", "set_mode", "scan",
                     "track", "observe", "hover", "wait", "report", "transit", "return", "land"}
FLIGHT_ACTIONS = GEOMETRY_ACTIONS | {"scan", "track", "observe", "hover", "wait", "transit", "return", "land"}
EVENTS = ("found", "not_found", "timeout")
LABELS = {"search": "搜索/探索", "patrol": "边界巡逻", "record_start": "开启录像",
          "record_stop": "停止录像", "photo": "拍照", "set_mode": "切换模式", "scan": "扫描",
          "track": "跟踪", "observe": "持续观察", "hover": "悬停", "wait": "等待",
          "report": "报告", "transit": "飞往", "return": "返航", "land": "降落"}
EVENT_LABELS = {"completed": "完成后", "found": "发现目标后", "not_found": "搜索结束未发现目标后", "timeout": "超时后"}


def _invalid(message, **details):
    raise AtomicError("INVALID_ARGUMENT", message, **details)


def references(value):
    """Yield event-result references, deliberately distinct from artifact ref/select."""
    if isinstance(value, dict):
        if "from_step" in value:
            if set(value) != {"from_step", "field"} or value.get("field") != "target_position":
                _invalid("动态引用须为 {from_step, field: target_position}")
            yield value
        else:
            for item in value.values():
                yield from references(item)
    elif isinstance(value, list):
        for item in value:
            yield from references(item)


def validate(request):
    """Validate graph identity, dependencies, event gates and runtime references."""
    require(request, "input", dict)
    allowed = {"nodes", "edges", "flight_alt", "speed", "work_speed", "search_timeout", "uav_ids", "payload",
               "exclude_uavs", "constraints", "solver_options", "max_duration_s", "runtime_bindings",
               "observed_events", "return_to_home"}
    if set(request) - allowed:
        _invalid("任务图含不支持的字段；循环任务需先展开为有界步骤", fields=sorted(set(request) - allowed))
    if "return_to_home" in request and type(request["return_to_home"]) is not bool:
        _invalid("return_to_home须为布尔值")
    raw = require(required(request, "nodes"), "nodes", list)
    if not 1 <= len(raw) <= 64:
        _invalid("nodes 数量须为1..64；循环任务需有明确停止条件")
    altitude = finite(request.get("flight_alt", DEFAULT_REF_ALT_M), "flight_alt", True)
    # Exploration boxes have a fixed lower z of 0.3 m. Legacy low-level
    # geometry remains permissive; this mission entry enforces its actual floor.
    if altitude <= 0.3:
        _invalid("任务航高须大于0.3米；上限由参与机型升限决定")
    for key in ("speed", "work_speed", "search_timeout", "max_duration_s"):
        if key in request:
            finite(request[key], key, True)
    nodes, indexed = [], {}
    for item in raw:
        require(item, "node", dict)
        if set(item) - {"id", "action", "uav_id", "job", "params", "join"}:
            _invalid("节点含不支持的字段", fields=sorted(set(item) - {"id", "action", "uav_id", "job", "params", "join"}))
        node = deepcopy(item)
        nid = required(node, "id")
        if not isinstance(nid, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", nid) or nid in indexed:
            _invalid("节点id须唯一，使用字母开头的字母数字、下划线或连字符")
        action = required(node, "action")
        if not isinstance(action, str) or action not in GEOMETRY_ACTIONS | INTERFACE_ACTIONS:
            _invalid("不支持的规划动作", node_id=nid, action=action)
        if "uav_id" in node and (not isinstance(node["uav_id"], str) or not node["uav_id"].strip()):
            _invalid("uav_id须为非空机号", node_id=nid)
        node.setdefault("join", "all")
        if node["join"] not in ("all", "any"):
            _invalid("join须为all或any", node_id=nid)
        params = require(node.setdefault("params", {}), "params", dict)
        permitted_params = {"duration_s", "target", "targets", "mode", "description", "until_step"}
        if set(params) - permitted_params:
            _invalid("步骤参数尚未支持，不能忽略用户要求", node_id=nid, fields=sorted(set(params) - permitted_params))
        if "duration_s" in params:
            finite(params["duration_s"], "duration_s", True)
        if action == "set_mode" and (not isinstance(params.get("mode"), str) or not params["mode"].strip()):
            _invalid("切换模式需要mode", node_id=nid)
        if action in GEOMETRY_ACTIONS:
            job = require(required(node, "job"), "job", dict)
            if any(key in job for key in ("job_id", "uav_id", "task_type")):
                _invalid("作业身份、机号和类型须在节点层指定", node_id=nid)
            if "duration_s" in params:
                _invalid("区域任务的时限应写入job.search_timeout，巡逻用job.laps", node_id=nid)
            if "center_from" in job:
                ref = job["center_from"]
                if not isinstance(ref, dict) or set(ref) != {"from_step", "field"}:
                    _invalid("center_from须为动态目标位置引用", node_id=nid)
                if set(job) - {"center_from", "shape", "radius", "length", "width", "name", "path_mode", "entry_side", "direction", "laps", "search_timeout"}:
                    _invalid("动态区域含不支持或冲突的字段", node_id=nid)
                shape = job.get("shape")
                if shape not in ("circle", "rectangle"):
                    _invalid("动态目标区域也需要明确形状和尺寸", node_id=nid)
                for field in (["radius"] if shape == "circle" else ["length", "width"]):
                    finite(required(job, field), field, True)
        elif "job" in node:
            _invalid("该动作不接收区域job；位置或对象写入params.target", node_id=nid)
        nodes.append(node)
        indexed[nid] = node
    edges = require(request.get("edges", []), "edges", list)
    if len(edges) > 256:
        _invalid("任务依赖最多256条")
    incoming, outgoing = {n: [] for n in indexed}, {n: [] for n in indexed}
    normalized, seen = [], set()
    for item in edges:
        require(item, "edge", dict)
        if set(item) - {"from", "to", "on"}:
            _invalid("依赖只接收from、to和on")
        a, b, event = required(item, "from"), required(item, "to"), item.get("on", "completed")
        if not isinstance(a, str) or not isinstance(b, str) or a not in indexed or b not in indexed or a == b:
            _invalid("依赖须连接两个存在且不同的节点")
        if not isinstance(event, str) or event not in ("completed", *EVENTS):
            _invalid("未知的触发事件", event=event)
        if event in EVENTS and indexed[a]["action"] != "search":
            _invalid("found/not_found/timeout事件须来自搜索节点", node_id=a)
        key = (a, b, event)
        if key in seen:
            _invalid("重复的任务依赖")
        seen.add(key)
        edge = {"from": a, "to": b, "on": event}
        normalized.append(edge)
        incoming[b].append(edge)
        outgoing[a].append(edge)
    # Stable topological order also defines deterministic tie-breaking when
    # two ready operations compete for the same aircraft.
    degree = {n: len(incoming[n]) for n in indexed}
    order = []
    while len(order) < len(nodes):
        ready = [n for n in indexed if degree[n] == 0 and n not in order]
        if not ready:
            _invalid("任务依赖形成循环，无法规划")
        for nid in ready:
            order.append(nid)
            for edge in outgoing[nid]:
                degree[edge["to"]] -= 1
    ancestors = {}
    for nid in order:
        ancestors[nid] = set()
        for edge in incoming[nid]:
            ancestors[nid].add(edge["from"])
            ancestors[nid].update(ancestors[edge["from"]])
        for ref in references(indexed[nid]):
            source = ref["from_step"]
            if not isinstance(source, str) or source not in ancestors[nid] or indexed[source]["action"] != "search":
                _invalid("动态目标位置必须引用前置搜索节点", node_id=nid)
        until = indexed[nid]["params"].get("until_step")
        if until is not None and (indexed[nid]["action"] != "record_start" or not isinstance(until, str)
                                  or until not in indexed or until == nid):
            _invalid("until_step只用于录像，且须指向其他已有步骤", node_id=nid)
    for nid in order:
        until = indexed[nid]["params"].get("until_step")
        if until is not None and nid not in ancestors[until]:
            _invalid("录像结束步骤必须依赖录像开始步骤", node_id=nid)
    observed = require(request.get("observed_events", {}), "observed_events", dict)
    for nid, event in observed.items():
        if nid not in indexed or indexed[nid]["action"] != "search" or event not in EVENTS:
            _invalid("observed_events须引用搜索节点及有效结果")
    bindings = require(request.get("runtime_bindings", {}), "runtime_bindings", dict)
    for nid, result in bindings.items():
        if nid not in indexed or indexed[nid]["action"] != "search" or observed.get(nid) != "found":
            _invalid("目标位置绑定需要同一搜索节点的found事件", node_id=nid)
        require(result, "runtime_bindings[]", dict)
        if set(result) != {"target_position"}:
            _invalid("结果绑定仅接收target_position")
        position = require(result["target_position"], "target_position", dict)
        if set(position) != {"longitude", "latitude"}:
            _invalid("目标位置需要longitude和latitude")
        lon, lat = finite(position["longitude"], "longitude"), finite(position["latitude"], "latitude")
        if not -180 <= lon <= 180 or not -90 <= lat <= 90:
            _invalid("目标位置超出WGS84范围")
    return {"nodes": nodes, "edges": normalized, "order": order, "incoming": incoming,
            "flight_alt": altitude, "indexed": indexed}


def scenarios(graph, observed=None):
    """Enumerate only reachable result combinations; never run exclusive branches together."""
    observed = observed or {}
    event_sources = {e["from"] for e in graph["edges"] if e["on"] in EVENTS}
    event_sources.update(r["from_step"] for node in graph["nodes"] for r in references(node))
    event_sources.update(observed)
    variants = [{"events": {}, "active": []}]
    for nid in graph["order"]:
        next_variants = []
        node = graph["indexed"][nid]
        for variant in variants:
            checks = [e["from"] in variant["active"] and
                      (e["on"] == "completed" or variant["events"].get(e["from"]) == e["on"])
                      for e in graph["incoming"][nid]]
            active = (all(checks) if node["join"] == "all" else any(checks)) if checks else True
            if not active:
                if nid in observed:
                    # This observation rules out this counterfactual path.
                    continue
                next_variants.append(variant)
                continue
            choices = [observed[nid]] if nid in observed else (EVENTS if nid in event_sources else [None])
            for event in choices:
                events = dict(variant["events"])
                if event is not None:
                    events[nid] = event
                next_variants.append({"events": events, "active": variant["active"] + [nid]})
        if len(next_variants) > 64:
            _invalid("可达条件分支超过64种，请拆分任务或提供已知事件")
        variants = next_variants
    if not variants:
        _invalid("观测事件相互矛盾，没有可达分支")
    if not observed:
        reachable = {nid for variant in variants for nid in variant["active"]}
        if reachable != set(graph["order"]):
            _invalid("存在永远无法触发的步骤，请检查条件与汇合规则", nodes=sorted(set(graph["order"]) - reachable))
    # An available target position requires an active found gate in every
    # scenario using it. A plain completed edge must not pretend one exists.
    for variant in variants:
        for nid in variant["active"]:
            until = graph["indexed"][nid]["params"].get("until_step")
            if until is not None and until not in variant["active"]:
                _invalid("录像结束步骤在本分支不会启动", node_id=nid)
            for ref in references(graph["indexed"][nid]):
                if variant["events"].get(ref["from_step"]) != "found":
                    _invalid("使用目标位置的步骤必须受found分支约束", node_id=nid)
    return variants


def bind(value, bindings):
    if isinstance(value, dict):
        if "from_step" in value:
            return deepcopy(bindings.get(value["from_step"], {}).get(value["field"], value))
        return {k: bind(v, bindings) for k, v in value.items()}
    if isinstance(value, list):
        return [bind(v, bindings) for v in value]
    return deepcopy(value)
