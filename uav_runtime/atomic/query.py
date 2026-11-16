"""Read facts from the configured immutable planning snapshot."""
from contextlib import closing
import sqlite3
from uav_runtime.paths import DATA
from uav_runtime.snapshot_sql import SnapshotError, select
from .common import AtomicError, finite


def database_path(table):
    path = DATA / (table + ".db")
    if not path.is_file():
        raise AtomicError("MISSING_DATABASE", f"请将 {table}.db 放入 {DATA}")
    return path


def rows(table, bounds=None, ids=None):
    path = database_path(table)
    clauses = []
    parameters = []
    if bounds and table in ("uav_status", "geo_library"):
        clauses.append("longitude BETWEEN ? AND ? AND latitude BETWEEN ? AND ?")
        parameters.extend([bounds["min_lon"], bounds["max_lon"],
                           bounds["min_lat"], bounds["max_lat"]])
    if ids is not None and table in ("uav_status", "uav_resource"):
        ids = sorted(set(ids))
        if not ids:
            return []
        clauses.append("uav_id IN (" + ",".join("?" * len(ids)) + ")")
        parameters.extend(ids)
    sql = "SELECT * FROM " + table
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            records = [dict(row) for row in db.execute(sql, parameters)]
    except sqlite3.Error as exc:
        raise AtomicError("INVALID_DATABASE", f"无法读取 {path} 的 {table} 表：{exc}") from exc
    if table in ("uav_status", "uav_resource"):
        ids = [row.get("uav_id") for row in records]
        if any(not isinstance(uid, str) or not uid.strip() for uid in ids) or len(ids) != len(set(ids)):
            raise AtomicError("INVALID_DATABASE", f"{table} 需要唯一且非空的 uav_id")
    return records


def compute(request):
    """查询 entity=fleet/places/status/resources，可指定 uav_ids、name、fields。"""
    if not isinstance(request, dict) or set(request) - {"entity", "uav_ids", "name", "fields", "sql", "parameters", "bounds"}:
        raise AtomicError("INVALID_ARGUMENT", "查询输入含未知字段或不是对象")
    entity = request.get("entity", "fleet")
    truncated = False
    bounds = request.get("bounds")
    if bounds is not None:
        if not isinstance(bounds, dict) or set(bounds) != {"min_lon", "max_lon", "min_lat", "max_lat"}:
            raise AtomicError("INVALID_ARGUMENT", "bounds 须为 min_lon/max_lon/min_lat/max_lat")
        min_lon = finite(bounds["min_lon"], "bounds.min_lon")
        max_lon = finite(bounds["max_lon"], "bounds.max_lon")
        min_lat = finite(bounds["min_lat"], "bounds.min_lat")
        max_lat = finite(bounds["max_lat"], "bounds.max_lat")
        if not (-180 <= min_lon <= max_lon <= 180 and -90 <= min_lat <= max_lat <= 90):
            raise AtomicError("INVALID_ARGUMENT", "bounds 超出 WGS84 范围或顺序不正确")
        bounds = {"min_lon": min_lon, "max_lon": max_lon,
                  "min_lat": min_lat, "max_lat": max_lat}
    if "sql" in request:
        tables = {"status": "uav_status", "resources": "uav_resource", "places": "geo_library"}
        if entity not in tables or set(request) & {"uav_ids", "name", "fields", "bounds"}:
            raise AtomicError("INVALID_ARGUMENT", "sql 仅支持 status/resources/places，与 uav_ids/name/fields/bounds 互斥")
        try:
            data, truncated = select(database_path(tables[entity]), tables[entity], request["sql"], request.get("parameters", []))
        except SnapshotError as exc:
            raise AtomicError(exc.code, str(exc)) from exc
        except ValueError as exc:
            raise AtomicError("INVALID_ARGUMENT", str(exc)) from exc
        if not data:
            raise AtomicError("NOT_FOUND", "只读 SQL 没有返回记录")
        key = "records"
    elif "parameters" in request:
        raise AtomicError("INVALID_ARGUMENT", "parameters 只与 sql 一起使用")
    elif entity == "fleet":
        if bounds:
            # Only the in-box status rows participate; resource-only records
            # have no position and cannot be inside a spatial query.
            states = {r["uav_id"]: r for r in rows("uav_status", bounds)}
            resources = {r["uav_id"]: r for r in rows("uav_resource", ids=states)}
            data = []
            for uid in sorted(states):
                row = {**resources.get(uid, {}), **states[uid], "uav_id": uid,
                       "has_status": True, "has_resource": uid in resources}
                row["origin"] = {"longitude": states[uid].get("longitude"),
                                 "latitude": states[uid].get("latitude")}
                data.append(row)
        else:
            states = {r["uav_id"]: r for r in rows("uav_status")}
            resources = {r["uav_id"]: r for r in rows("uav_resource")}
            data = []
            for uid in sorted(set(states) | set(resources)):
                row = {**resources.get(uid, {}), **states.get(uid, {}), "uav_id": uid,
                       "has_status": uid in states, "has_resource": uid in resources}
                if uid in states:
                    row["origin"] = {"longitude": states[uid].get("longitude"), "latitude": states[uid].get("latitude")}
                data.append(row)
        key = "fleet"
    elif entity in ("status", "resources", "places"):
        if bounds and entity == "resources":
            raise AtomicError("INVALID_ARGUMENT", "资源表没有经纬度，bounds 只用于 status/places/fleet")
        table = {"status": "uav_status", "resources": "uav_resource", "places": "geo_library"}[entity]
        data = rows(table, bounds)
        key = "statuses" if entity == "status" else entity
    else:
        raise AtomicError("INVALID_ARGUMENT", "entity 须为 fleet、places、status 或 resources")
    ids = request.get("uav_ids")
    if ids is not None:
        if entity == "places" or not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
            raise AtomicError("INVALID_ARGUMENT", "uav_ids 是无人机编号数组")
        unknown = set(ids) - {r["uav_id"] for r in data}
        if unknown:
            raise AtomicError("NOT_FOUND", "无人机无记录", uav_ids=sorted(unknown))
        data = [r for r in data if r["uav_id"] in ids]
    name = request.get("name")
    if name is not None:
        if entity != "places" or not isinstance(name, str) or not name.strip():
            raise AtomicError("INVALID_ARGUMENT", "name 只用于地点名称或别名")
        lookup = name.strip().casefold()
        matches = [r for r in data if lookup in [label.strip().casefold() for label in [r["region_name"], *(r.get("alias") or "").split(";")]]]
        if not matches:
            suggestions = sorted({r["region_name"] for r in data if name in r["region_name"] or r["region_name"] in name})
            raise AtomicError("NOT_FOUND", "地点没有精确匹配", suggestions=suggestions)
        data = matches
    fields = request.get("fields")
    if fields is not None:
        if not isinstance(fields, list) or not fields or not all(isinstance(f, str) for f in fields):
            raise AtomicError("INVALID_ARGUMENT", "fields 须为非空字段名数组")
        known = set().union(*(r.keys() for r in data))
        if set(fields) - known:
            raise AtomicError("INVALID_ARGUMENT", "字段不存在", fields=sorted(set(fields) - known))
        data = [{k: r.get(k) for k in fields} for r in data]
    units = {"longitude": "degrees", "latitude": "degrees", "altitude": "m",
             "flight_remain": "m (剩余可飞距离，不是秒)", "max_endurance": "min (机型满电最大续航，不是剩余时间)",
             "battery_level": "%", "max_range": "m", "max_range_full": "m", "max_range_empty": "m",
             "max_altitude": "m", "region_length_m": "m", "region_width_m": "m"}
    present = set().union(*(row.keys() for row in data))
    units = {k: v for k, v in units.items() if k in present}
    return {"status": "OK", key: data, "source": "configured_static_snapshot", "count": len(data), "units": units,
            "truncated": truncated, "bounds": bounds,
            "_summary": {"entity": entity, "count": len(data), "records": data, "units": units,
                         "truncated": truncated, "bounds": bounds}}
