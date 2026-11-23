# 完整规划的成本与航段

仅计算完整方案的 costs/build 时读；单项两点或区域航迹预览使用 offline-preview.md。

```bash
uav_runtime/.venv/bin/python uav_runtime/run.py skills/trajectory-generation/scripts/planning_routes.py --input '<JSON对象>'
```

也可用 `--input-file <工作区内绝对路径>`，与 `--input` 互斥。成功返回 `artifact_file`、`data_fields` 和 `summary`，完整结果在该文件的 `data` 中。任何输入字段可用 `{"ref":"实际返回的artifact_file","select":"/data/字段"}` 引用；大矩阵和完整分配记录直接引用。

连续的候选、区域、成本、分配、航迹和动作计算优先用 [产物串联入口](../../mission-planning/references/artifact-pipeline.md)，通过 `ref_step` 传递产物，避免手抄文件名。此入口只串联本地计算；条件分支和动态目标位置使用 [任务图](../../mission-planning/references/task-graphs.md)。

`operation=costs` 接收候选记录和已解析区域：

`required_drones`、`allowed_assignments`、`single_owner_jobs` 属于后续 `allocate` 的 `constraints`，不传给 `costs`；`costs` 会从作业生成固定归属及圆形巡逻独占约束。分配时合并成本产物已有约束和用户要求，不覆盖已有约束。

```json
{
  "operation": "costs",
  "candidates": {"ref": "候选查询实际返回的文件", "select": "/data/candidates"},
  "jobs": {"ref": "区域解析实际返回的文件", "select": "/data/jobs"},
  "task_type": "search",
  "ordered": false
}
```

- 顶层 `task_type` 为 `search`（含无目标探索）或 `patrol`；`flight_alt` 默认1.2 m，是错高基准；`laps` 默认1且只用于巡逻；`speed` 可选，省略时按逐机 `cruise_speed` 规划（缺失回退 1.2 m/s）；`max_speed` 只做上限，不作为规划速度。
- 搜索航带按 `lane = 2 * flight_alt * tan(fov/2)` 估算；固定作业用该机 FOV，非固定作业用候选中最小 FOV（保守）。矩形覆盖按长边铺条带：`n=ceil(短边/lane)`，`coverage=n*长边+(n-1)*lane`。这不是固定 `lane=2 m`；单项 `query-area --lane` 的默认 2 m 只用于只读预览，不用于完整任务成本。
- 每个 job 可带 `task_type`、`laps`、`search_timeout`，覆盖顶层默认值；同一区域先巡逻再搜索时写两个 job。`ordered=true` 时 `build` 按 jobs 顺序排列每架机的作业；只保证同一架机内的顺序，跨机固定顺序仍不支持。多步骤任务先用 `check_task.py` 核对，详见 [多步骤任务](../../mission-planning/references/combined-tasks.md)。
- `laps` 是同一次任务内连续巡逻的圈数；当前完整任务入口未提供定时启动、间隔等待或跨趟补能的周期调度，须说明接口缺口，并完成可独立计算的单趟只读预览。保留原单趟圈数、间隔和持续时间，执行替代方案需用户接受。
- candidates 包含 `uav_id,origin,range_budget,time_budget,battery,flight_remain,max_altitude,max_speed,cruise_speed,planning_speed,speed_source`；jobs 包含 `job_id,name,center,shape` 和形状尺寸，可带 `entrance,fixed_uav,area_source`、`task_type,laps,search_timeout`。地点及机队数据由调用方提供，本入口不查询数据库。
- `costs` 可接收最多4096架完整候选，按区域外接圆的往返距离/时间下界、机型半径和指定速度上限预筛；直接引用完整候选，后续分配用成本产物的候选集。非固定搜索只保留一个候选时按其 FOV/速度重算工作量与时间，多机共担仍是保守估计。筛后超过64架返回 `CANDIDATE_LIMIT_EXCEEDED`：这是求解规模限制，不截取前/最近64架或自定阈值。固定归属预算必超或逐作业允许集合与候选交集为空时报告约束不可行，不静默改约束。`candidate_screening` 的详细字段和失败解释仅失败/排错时读 [阶段诊断](../../mission-planning/references/diagnostics.md#参数与成本)。
- 成本或预算失败时，读 [参数与成本诊断](../../mission-planning/references/diagnostics.md#参数与成本)，按本次证据解释绑定约束、速度或航高备选；不静默采用改变要求的方案。
- 返回 `/data/candidates`、`/data/jobs`（求解用编号与名称）、`/data/costs`、`/data/constraints`，可原样用于分配求解。`costs.reachable` 按候选机和作业给出**实际往返转场**可达集合：只有从起飞点飞抵该作业并返回都在航程与时间预算内的作业才列入；不可达作业的转场弧写成 `null`，没有任何可达作业的候选会从成本候选集中移除（见 `reachable_count` / `cost_excluded_count`）。约束保留区域的 `fixed_uav`；**圆形巡逻**要求单机负责，以保留完整圆周；**圆形搜索**会转成外接矩形搜索框，可由两机共担，不强制单机。用户另有数量或候选限制时须一并表达。
- `/data/prepared` 保存完整区域、候选和实际参数；`prepared_digest`用于发现与成本记录不一致的修改。覆盖轨迹只是航程估计，不代表保证覆盖完成。

`operation=build` 接收完整成本记录和完整分配结果：

```json
{
  "operation": "build",
  "cost_estimate": {"ref": "成本实际返回的文件", "select": "/data"},
  "allocation": {"ref": "分配实际返回的文件", "select": "/data"},
  "search_timeout": 240
}
```

- `search_timeout` 默认1200秒，仅覆盖未写该字段的搜索作业；航高、每步类型、圈数、速度、区域和顺序沿用成本记录，不能在此覆盖。改这些输入后需要相应的新成本及分配证据。
- 校验分配的真实求解输入、结果、索引、预算、成本、约束和访问顺序；不手改归属。按参与机编号依次在基准航高上增加0.3 m，并检查每架机上限；严格固定同高的多机请求不能用该错高策略代替。
- 返回 `task_key,jobs,assignments,unused,solver`。每项 assignment 含 `uav_id,origin,flight_alt,jobs,steps,estimated_range_m,estimated_battery_after`。steps 为 `transit`、`search` 或 `patrol`，兼容 flight-control 的动作组装输入；搜索步含 `box_min,box_max,timeout_sec`，巡逻步含 `waypoints,laps`。同一架机可混合 search 和 patrol 步，按各步类型和参数处理。
- 命令响应的 `summary.assignments`（保存产物中为 `data.assignments`）同时给出每架执行机的 `battery_level`（原始电量）、`flight_remain`（原始剩余航程）、`range_budget/time_budget`（预留后预算）和 `estimated_battery_after`（预计剩余电量）；报告按同一机号直接读取这些值。
- 最终分区航程须满足输入航程预算且剩余电量大于20%。该估计只覆盖水平转场和覆盖/巡逻长度；不包含起降、搜索超时上界或跟踪占用，`whole_mission_budget_complete=false`，不能称全任务续航已验证。

build 失败按 [组装与航迹诊断](../../mission-planning/references/diagnostics.md#动作组装与校验) 解释证据不一致、错高或预算问题，不修改归属或放宽参数求成功。
