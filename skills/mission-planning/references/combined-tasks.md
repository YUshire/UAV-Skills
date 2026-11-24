# 同机多步与混合任务 SOP

> 仅用于**同一架机内**混合 `search`/`patrol`、不同圈数/时限或明确“先…再…”顺序。
> **纯飞行红线**：`check_task.py` 与普通 `--pipeline` 仅支持 `search`/`patrol`，**严禁将纯“飞往/去某地”改为 `search`**。凡含纯飞行途经点或跨机先后依赖，直接改走 [task-graphs.md](task-graphs.md)（`transit` 节点或跨机 `completed` 边），勿读本页。

## 3 步执行流程

1. **先做步骤能力核对（`check_task.py`）**：
   一个动作步骤对应一个作业（同一地点先搜再巡拆成 `J1`/`J2` 两个作业）；仅把有实际先后依赖的步骤链传入：
   ```bash
   uav_runtime/.venv/bin/python uav_runtime/run.py skills/mission-planning/scripts/check_task.py \
     --input '{"steps":[{"job_id":"J1","task_type":"search","search_timeout":900},{"job_id":"J2","task_type":"patrol","laps":2}],"ordered":true}'
   ```
   - `route=full` 且 `gaps=[]` → 进入第 2 步；
   - `gaps` 含 `CROSS_UAV_ORDER`（跨机先后）→ 直接转 [task-graphs.md](task-graphs.md)；其他不支持项按 `gaps` 说明缺口。
2. **并入一次 `--pipeline` 规划**（按 [artifact-pipeline.md](artifact-pipeline.md)）：
   - 在 `resolve_jobs` 每项作业分别写 `job_id`、`task_type`、`laps`（仅 `patrol`）、`search_timeout`（仅 `search`）和可选 `uav_id`；
   - 同机需保序时在 `costs` 设 `"ordered": true`（若同机先后与其他机独立并行并存，`ordered: true` 只约束同机步骤顺序，归属与架数仍写进 `allocate.extra_constraints`）。
3. **整体校验交付**：以整任务 `--pipeline` 最终 `assemble_plan` 通过为准（单段分别通过不代表组合任务通过），按 [plan-delivery.md](plan-delivery.md) 交付。
