# 多步骤与混合任务

一个动作步骤对应一个作业（`jobs[]` 的一项）。同一地点先巡逻再搜索，也拆成两个作业；不要把一个地点写成一项再改全局 `task_type`。

## 任务表示

每个作业可带：

| 字段 | 说明 |
|---|---|
| `job_id` | 唯一编号，保留步骤身份和顺序 |
| `task_type` | `search` 或 `patrol`；省略时用顶层默认值 |
| `laps` | 巡逻圈数，只用于 `patrol`；省略时用顶层 `laps` 或 1 |
| `search_timeout` | 搜索时限，只用于 `search`；省略时用 `build.search_timeout` 或 1200 |
| `uav_id` | 用户明确指定执行机时写入 |

顶层 `task_type`、`laps` 只是未写这些字段的作业的默认值。需要保留用户“先……再……”的顺序时，在 `planning_routes.py` 的 `costs` 输入加 `"ordered": true`；`build` 会按 `jobs[]` 顺序排列每架机的作业。未要求顺序时省略或写 `false`，由分配算法决定访问顺序。

同一中心、两种动作：

```json
{
  "jobs": [
    {"job_id": "J1", "place": "三芝天弓导弹防空阵地", "task_type": "patrol", "laps": 2},
    {"job_id": "J2", "place": "三芝天弓导弹防空阵地", "task_type": "search", "search_timeout": 900}
  ],
  "ordered": true
}
```

两个地点、不同类型：

```json
{
  "jobs": [
    {"job_id": "J1", "place": "教学楼", "task_type": "search"},
    {"job_id": "J2", "place": "广场", "shape": "circle", "radius": 30, "task_type": "patrol", "laps": 1}
  ],
  "ordered": true
}
```

## 先核对能力

规划前用 `skills/mission-planning/scripts/check_task.py` 检查步骤；它把三类问题分开报告：

- `checks.mixed_action_types`：步骤类型集合，以及是否混合；
- `checks.mixed_parameters`：各步 `laps`、`search_timeout` 的不同取值；
- `checks.explicit_order`：是否要求顺序、顺序范围是单机内还是跨机。

`route=full` 且 `gaps=[]` 才按完整任务继续。`route=preview` 时按 `gaps` 说明缺口，只预览可独立完成的步骤，不把分段预览当成整任务通过。

```bash
uav_runtime/.venv/bin/python uav_runtime/run.py skills/mission-planning/scripts/check_task.py \
  --input '{"steps":[{"job_id":"J1","task_type":"patrol","laps":2},{"job_id":"J2","task_type":"search"}],"ordered":true}'
```

当前支持：

- 同一任务混合 `search` 和 `patrol`；
- 各步不同圈数、搜索时限；
- 同一架机内的步骤顺序（`ordered=true`，`build` 按 `jobs[]` 排序）。

旧扁平入口仍不支持：固定到不同无人机的步骤之间的先后依赖（检查器返回 `CROSS_UAV_ORDER`）。这种任务不能靠拆成两个独立任务后声称原顺序已满足。

## 下游按步骤处理

1. `resolve_jobs.py` 保留每步的 `task_type`、`laps`、`search_timeout`。
2. `planning_routes.py operation=costs` 按每步类型生成覆盖/巡逻航程，按每步参数计算工作时间和单机约束；混合类型不再要求全局唯一 `task_type`。
3. `allocate.py` 仍按作业分配归属；`ordered=true` 时 `build` 按 `jobs[]` 顺序排列该机的作业。
4. `planning_routes.py operation=build` 逐作业生成 `transit`、`search` 或 `patrol` 航段，保留 `job_id`、类型、圈数和时限。
5. `assemble_plan.py` 逐航段组装动作，`search` 用 `fuel_explore`，`patrol` 按该步 `laps` 展开 `patrol_move`。

两段分别通过成本、分配或 dry-run，不等于组合任务通过。最后仍要检查整任务的预算、航高、可达性、顺序和动作衔接。

同机先后与其他机独立并行并存时，`ordered=true` 只约束同机的步骤顺序；能力检查按实际有先后依赖的步骤链调用，不把独立并行步骤加入跨机先后链。归属与出动数量写入 `allocate.constraints`，不传给 `costs`；需跨机依赖时改用下面的任务图。

跨机依赖、并行、条件分支与预留能力步骤使用新增 [任务图规划入口](task-graphs.md)。原 check_task 和原普通任务输出保持兼容；任务图的几何前段校验不代表整个条件任务可以直接下发。
