# 单步成本与航段接口（`planning_routes.py`）

> 仅在手动单独调用 `costs`/`build` 时读；连续普通规划优先用 [artifact-pipeline.md](../../mission-planning/references/artifact-pipeline.md) 的 `--pipeline`，单项航迹预览用 [offline-preview.md](offline-preview.md)。

```bash
uav_runtime/.venv/bin/python uav_runtime/run.py skills/trajectory-generation/scripts/planning_routes.py --input '<JSON>'
```
（与 `--input-file <路径>` 互斥；支持 `{"ref":"<artifact_file>","select":"/data/..."}` 引用上游产物。）

## 1. `operation=costs`
```json
{
  "operation": "costs",
  "candidates": {"ref": "<candidates产物>", "select": "/data/candidates"},
  "jobs": {"ref": "<jobs产物>", "select": "/data/jobs"},
  "task_type": "search",
  "flight_alt": 50.0,
  "ordered": false
}
```
- `task_type`（`search`/`patrol`）、`flight_alt`（未指定时省略，默认 `min(50 m, max_altitude)`）、`laps`（默认 1，仅巡逻）、`speed`（可选，省略按逐机 `cruise_speed`）均可作为顶层默认，作业级 `task_type`/`laps`/`search_timeout` 优先。
- `required_drones`/`allowed_assignments`/`single_owner_jobs` 不传给 `costs`，留给后续 `allocate` 的 `extra_constraints`。
- 最多接收 4096 候选并自动做往返下界、飞行半径与速度上限预筛（筛后 >64 报 `CANDIDATE_LIMIT_EXCEEDED`，勿手工截前 64 架）。圆形巡逻自动生成单机约束，圆形搜索允许两机共担。
- 失败时按 `candidate_screening` 读 [diagnostics.md](../../mission-planning/references/diagnostics.md)。

## 2. `operation=build`
```json
{
  "operation": "build",
  "cost_estimate": {"ref": "<costs产物>", "select": "/data"},
  "allocation": {"ref": "<allocation产物>", "select": "/data"},
  "search_timeout": 1200
}
```
- 仅 `search_timeout` 可在 `build` 覆盖，其余参数沿用 `cost_estimate`。多机按编号间隔 0.3m 自动错高并核对机型升限；最终分区须满足航程/时间预算且剩余电量 >20%。
- 返回 `summary` 含 `task_key`、`assignments`、`unused`、`budget_scope`（仅水平估算，不含起降/搜索超时上界/跟踪占用，`whole_mission_budget_complete=false`、`map_used=false`）。
