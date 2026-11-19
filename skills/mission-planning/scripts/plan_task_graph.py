#!/usr/bin/env python3
"""规划跨机依赖、并行、条件分支和预留接口步骤；不会下发飞行任务。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from uav_runtime.atomic.common import run
from uav_runtime.graph_planner import compute

if __name__ == "__main__":
    raise SystemExit(run(compute, "task_graph_plan"))
