"""uav_runtime.config — 从 uav_status.db 读取 UAV 配置。

来源说明：原实现未随工作区提供（/data02 全盘不存在）。
本文件按调用方实际用到的接口重建，字段取自 data/uav_status.db：
  load_uavs()        -> {uav_id: config}
  load_uavs(path)    -> 同上，从 JSON 文件读
  get_uav(uav_id)    -> config 或 None
  runtime_root()     -> Path，运行时锁/状态文件目录

调用方用到的 config 键（见 fleet_dispatch.py / submit_queue_batch.py）：
  origin_lat, origin_lon, namespace, uav_id
其中 origin_* 用登记位置（快照），namespace 用 uav_id 派生。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .paths import ROOT

DB = ROOT / "data" / "uav_status.db"


def runtime_root() -> Path:
    """运行时状态目录（锁文件、预约表）。"""
    p = ROOT / "artifacts" / "runtime"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _row_to_config(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    uav_id = d["uav_id"]
    return {
        "uav_id": uav_id,
        # 登记位置（快照记录，非实时位置）
        "origin_lat": d.get("latitude"),
        "origin_lon": d.get("longitude"),
        "origin_alt": d.get("altitude"),
        # ROS 命名空间：按 uav_id 派生，与原系统一致（UAV-001 -> uav_001）
        "namespace": str(uav_id).lower().replace("-", "_"),
        # 其余快照字段原样带出，供技能按需读取
        "battery_level": d.get("battery_level"),
        "mission_status": d.get("mission_status"),
        "payload_status": d.get("payload_status"),
        "health_score": d.get("health_score"),
        "flight_remain": d.get("flight_remain"),
        "max_range_full": d.get("max_range_full"),
        "max_range_empty": d.get("max_range_empty"),
        "fov": d.get("fov"),
    }


def load_uavs(config: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """返回 {uav_id: config}。

    config 为 None 时从 data/uav_status.db 读；
    否则视为 JSON 文件路径，格式 {uav_id: {...}} 或 [{uav_id:...}, ...]。
    """
    if config:
        raw = json.loads(Path(config).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return {str(k): dict(v) for k, v in raw.items()}
        if isinstance(raw, list):
            return {str(item["uav_id"]): dict(item) for item in raw}
        raise ValueError("UAV config must be a mapping or a list of mappings")

    if not DB.is_file():
        raise FileNotFoundError(f"UAV database not found: {DB}")

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM uav_status").fetchall()
    finally:
        conn.close()

    return {row["uav_id"]: _row_to_config(row) for row in rows}


def get_uav(uav_id: str, config: str | Path | None = None) -> dict[str, Any] | None:
    """按 ID 取单个 UAV 配置，不存在返回 None。"""
    return load_uavs(config).get(uav_id)
