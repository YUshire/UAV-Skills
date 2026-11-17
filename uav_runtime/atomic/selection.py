"""Evaluate candidate aircraft from supplied records; no resource reads."""
from copy import deepcopy

from uav_runtime.budget_math import RESERVE_BATTERY_PERCENT, available_range_m, available_time_s
from .common import AtomicError, finite, require, required


def _ids(value, name):
    require(value, name, list)
    if any(not isinstance(item, str) or not item.strip() for item in value) or len(value) != len(set(value)):
        raise AtomicError("INVALID_ARGUMENT", f"{name} 须为不重复的非空字符串数组")
    return value


def compute(data):
    """给定机队记录和筛选条件，返回候选机及预算。"""
    require(data, "input", dict)
    if set(data) - {"fleet", "uav_ids", "exclude_uavs", "payload", "flight_alt"}:
        raise AtomicError("INVALID_ARGUMENT", "筛选输入含不支持的字段")
    fleet = require(required(data, "fleet"), "fleet", list)
    records = {}
    for row in fleet:
        require(row, "fleet[]", dict)
        uid = required(row, "uav_id")
        if not isinstance(uid, str) or not uid.strip() or uid in records:
            raise AtomicError("INVALID_ARGUMENT", "fleet 中 uav_id 须为唯一非空字符串")
        records[uid] = row
    requested = _ids(data.get("uav_ids", []), "uav_ids")
    excluded_ids = set(_ids(data.get("exclude_uavs", []), "exclude_uavs"))
    payload = _ids(data.get("payload", []), "payload")
    altitude = finite(data.get("flight_alt", 1.2), "flight_alt", positive=True)
    candidates, excluded = [], []
    for uid in requested or sorted(records):
        row, reasons = records.get(uid), []
        if uid in excluded_ids:
            reasons.append("调用方排除")
        if row is None:
            reasons.append("机队快照无记录")
        else:
            if row.get("mission_status") != "空闲":
                reasons.append("任务状态不是空闲")
            numbers = {}
            for key in ("battery_level", "health_score", "flight_remain", "max_altitude", "max_endurance"):
                try:
                    numbers[key] = finite(row.get(key), key)
                except AtomicError:
                    reasons.append(f"{key} 记录无效")
            battery = numbers.get("battery_level", 0)
            health = numbers.get("health_score", 0)
            # A per-aircraft reserve is a safety floor, not a replacement for
            # the 20% minimum; an invalid record is excluded rather than guessed.
            reserve = RESERVE_BATTERY_PERCENT
            raw_reserve = row.get("return_reserve")
            if raw_reserve is not None:
                try:
                    reserve = max(RESERVE_BATTERY_PERCENT, finite(raw_reserve, "return_reserve"))
                except AtomicError:
                    reasons.append("返航预留记录无效")
            if battery > 0 and reserve >= battery:
                reasons.append("返航预留不低于当前电量")
            if not 30 < battery <= 100:
                reasons.append("电量须大于 30% 且不超过 100%")
            if not 60 < health <= 100:
                reasons.append("健康度须大于 60 且不超过 100")
            if numbers.get("flight_remain", 0) <= 0:
                reasons.append("剩余可飞距离须大于 0")
            if numbers.get("max_altitude", 0) < altitude:
                reasons.append("机型最大航高低于所需航高")
            if numbers.get("max_endurance", 0) <= 0:
                reasons.append("最大续航须大于 0")
            installed = row.get("payload_status", "")
            if not isinstance(installed, str):
                reasons.append("载荷记录无效")
            else:
                reasons.extend(f"缺少载荷：{item}" for item in payload if item not in installed)
            try:
                origin = require(row.get("origin"), "origin", dict)
                lon = finite(origin.get("longitude"), "origin.longitude")
                lat = finite(origin.get("latitude"), "origin.latitude")
                if not -180 <= lon <= 180 or not -90 <= lat <= 90:
                    raise AtomicError("INVALID_ARGUMENT", "原点超出 WGS84 范围")
            except AtomicError:
                reasons.append("起飞原点记录无效")
        if reasons:
            excluded.append({"uav_id": uid, "reasons": reasons})
            continue
        item = deepcopy(row)
        item.update(numbers)
        item["reserve_battery_percent"] = reserve
        item["range_budget"] = available_range_m(numbers["flight_remain"], battery, reserve)
        item["time_budget"] = available_time_s(numbers["max_endurance"], battery, reserve)
        candidates.append(item)
    altitudes = []
    for row in records.values():
        try:
            value = finite(row.get("max_altitude"), "max_altitude")
        except AtomicError:
            continue
        if value > 0:
            altitudes.append(value)
    max_recorded_altitude = max(altitudes) if altitudes else None
    if requested and excluded and not candidates:
        raise AtomicError("CANDIDATE_EXCLUDED", "指定无人机未通过筛选", excluded=excluded,
                          max_recorded_altitude=max_recorded_altitude)
    if not candidates:
        raise AtomicError("NO_CANDIDATE", "没有合格候选机", excluded=excluded,
                          max_recorded_altitude=max_recorded_altitude)
    summary = {"candidates": [{"uav_id": row["uav_id"], "range_budget": row["range_budget"],
                                "time_budget": row["time_budget"], "max_altitude": row["max_altitude"],
                                "reserve_battery_percent": row["reserve_battery_percent"]}
                               for row in candidates], "excluded": excluded}
    return {"status": "OK", "candidates": candidates, "excluded": excluded,
            "flight_alt": altitude, "reserve_battery_percent": RESERVE_BATTERY_PERCENT, "_summary": summary}
