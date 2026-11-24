---
name: mission-orchestration
description: 【纯数值分配咨询】负责：用户直接给定数值预算与成本矩阵时的独立分配求解（allocate）。不负责：常规完整任务规划（已由 mission-planning 的 --pipeline 与 extra_constraints 封装，勿单独读取）。
---

# 纯数值任务分配（`allocate.py`）

```bash
uav_runtime/.venv/bin/python uav_runtime/run.py skills/mission-orchestration/scripts/allocate.py --input-file artifacts/requests/<唯一名>.json
```
（也支持 `--input '<JSON>'`，互斥；大矩阵优先通过 `{"ref":"<artifact_file>","select":"/data/..."}` 引用。）

## 输入契约
- `candidates`（1..64 项）：`uav_id`、正数 `range_budget`(m)、`time_budget`(s)、可选 `planning_speed`(m/s)。
- `jobs`（1..64 项）：`job_id`、可选 `name`。
- `costs`：`transfer_range`/`transfer_time`（每机 `(N+1)×(N+1)` 矩阵，0 为基地，1..N 对应 `jobs`，不可达弧可为 `null` 但须由 `reachable` 排除）、`work_range`/`work_time`（N 项向量）、`reachable`（每机可达 `job_id` 数组）。
- 可选 `groups`（代替顶层 `jobs`/`costs`）：`[{group_id,jobs,costs,candidates?}]`，每机至多属一组。
- 可选 `constraints` / `extra_constraints`（`extra_constraints` 与 `constraints` 自动安全合并，保留已有约束不覆盖）：
  - `fixed_assignments:{job_id:uav_id}`（独占）、`allowed_assignments:{job_id:[uav_id]}`（允许候选集）、`single_owner_jobs:[job_id]`（单机负责）、`required_drones:N`（恰好 N 架实际作业）。未限定独占的作业最多两机共担。
- 可选 `solver_options`：`time_limit_sec`（默认 20，上限 120）、`weights`（`makespan,total_range,total_wait,uav_count`）。

## 输出与失败
- 成功返回 `summary`：`assignments`（`uav_id,job_ids,estimated_range_m`）、`owners`、`unused`、`solver`、`estimated_only=true`。
- 交付执行机、作业顺序、未用候选及条件估计说明（`estimated_range_m` 非最终航迹验证；`solver.optimal=false` 须说明“可行但未证明最优”）。
- 失败码：`NO_FEASIBLE_ALLOCATION`（含 `SHARED_JOB_TIME_BUDGET` 等诊断，详见 `diagnostics.md`）、`MISSING_PARAMETER`/`INVALID_ARGUMENT`/`MISSING_COST`/`BUDGET_MISMATCH`/`SOLVER_*`——如实报告，不删约束绕过。
