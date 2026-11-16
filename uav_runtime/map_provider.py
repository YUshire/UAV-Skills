"""uav_runtime.map_provider — 语义地图提供者。

来源说明：原实现未随工作区提供（/data02 全盘不存在）。
技能里只有一处用到：trajectory-generation/scripts/semantic_map.py
    from uav_runtime.map_provider import create_map as SemanticMap, map_context
    SemanticMap(args.world)

在线语义地图需要 ROS 组件，当前未部署（self_check 报
semantic_map_provider_present=false）。本模块提供**离线几何兜底**：
按 world 文件里的障碍多边形做射线求交，不依赖 ROS。

短处（shortcut）：仅支持 JSON world（obstacles 为 [[x,y],...] 多边形列表），
不支持 ROS 栅格地图；接入 ROS 时替换 _ObstacleIndex。
"""

from __future__ import annotations

import json
import math
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


def _segments_intersect(a, b, c, d) -> bool:
    """二维线段相交判断（含端点接触）。"""
    def cross(o, p, q):
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])

    d1, d2 = cross(c, d, a), cross(c, d, b)
    d3, d4 = cross(a, b, c), cross(a, b, d)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return True
    return False


class _ObstacleIndex:
    """把多边形障碍拆成线段，判断路径是否穿越。"""

    def __init__(self, polygons: list[list[list[float]]]) -> None:
        self.edges: list[tuple[tuple[float, float], tuple[float, float]]] = []
        for poly in polygons:
            if not isinstance(poly, list) or len(poly) < 3:
                continue
            pts = [(float(p[0]), float(p[1])) for p in poly]
            for i in range(len(pts)):
                self.edges.append((pts[i], pts[(i + 1) % len(pts)]))

    def blocked(self, start, end) -> bool:
        s = (float(start[0]), float(start[1]))
        e = (float(end[0]), float(end[1]))
        return any(_segments_intersect(s, e, a, b) for a, b in self.edges)

    def __len__(self) -> int:
        return len(self.edges)


class MapProvider:
    """离线几何地图。blocked(start, end) -> bool。"""

    def __init__(self, world: str | Path | None = None) -> None:
        self.world = Path(world) if world else None
        self.index = _ObstacleIndex(self._load(self.world))
        self.mock = True
        self.map_used = bool(self.world and self.world.is_file())

    @staticmethod
    def _load(world: Path | None) -> list[list[list[float]]]:
        if not world or not world.is_file():
            return []
        try:
            data = json.loads(world.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        obs = data.get("obstacles") if isinstance(data, dict) else data
        return obs if isinstance(obs, list) else []

    def blocked(self, start, end) -> bool:
        return self.index.blocked(start, end)

    def describe(self) -> dict[str, Any]:
        return {
            "world": str(self.world) if self.world else None,
            "obstacles": len(self.index),
            "mock": self.mock,
            "map_used": self.map_used,
        }


def create_map(world: str | Path | None = None) -> MapProvider:
    """技能调用的工厂函数。"""
    return MapProvider(world)


@contextmanager
def map_context(world: str | Path | None = None) -> Iterator[MapProvider]:
    """with map_context(world) as m: ..."""
    yield MapProvider(world)
