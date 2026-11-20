---
name: trajectory-generation
description: 使用本地离线几何计算单项航迹、转场与作业成本，并按求解归属和顺序生成每机航段、区域分工及航程估计。结果标记为mock，不校验地图障碍。
---

# 航迹查询

单项查询使用本地 `mock_offline_map.py`。任务规划使用 `planning_routes.py` 的 `costs` 和 `build` 两种操作：前者计算数值成本，后者按已有分配生成最终航迹；两者均不调用分配算法。这些入口无需设置环境变量。

## 任务规划的成本与航段

```bash
uav_runtime/.venv/bin/python uav_runtime/run.py skills/trajectory-generation/scripts/planning_routes.py --input '<JSON对象>'
```

也可用 `--input-file <工作区内绝对路径>`，与 `--input` 互斥。成功返回 `artifact_file`、`data_fields` 和 `summary`，完整结果在该文件的 `data` 中。任何输入字段可用 `{"ref":"实际返回的artifact_file","select":"/data/字段"}` 引用；大矩阵和完整分配记录直接引用。

连续的候选、区域、成本、分配、航迹和动作计算优先用 [产物串联入口](../mission-planning/references/artifact-pipeline.md)，通过 `ref_step` 传递产物，避免手抄文件名。此入口只串联本地计算；条件分支和动态目标位置仍按 mission-planning 使用任务图。

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
- 每个 job 可带 `task_type`、`laps`、`search_timeout`，覆盖顶层默认值；同一区域先巡逻再搜索时写两个 job。`ordered=true` 时 `build` 按 jobs 顺序排列每架机的作业；只保证同一架机内的顺序，跨机固定顺序仍不支持。多步骤任务先用 `check_task.py` 核对，详见 mission-planning 的 combined-tasks。
- `laps` 是同一次任务内连续巡逻的圈数；周期调度按 mission-planning 说明接口缺口，并完成可独立计算的单趟只读预览。保留原单趟圈数、间隔和持续时间，执行替代方案需用户接受。
- candidates 包含 `uav_id,origin,range_budget,time_budget,battery,flight_remain,max_altitude,max_speed,cruise_speed,planning_speed,speed_source`；jobs 包含 `job_id,name,center,shape` 和形状尺寸，可带 `entrance,fixed_uav,area_source`、`task_type,laps,search_timeout`。地点及机队数据由调用方提供，本入口不查询数据库。
- `costs` 可接收最多4096架完整候选，按区域外接圆的往返距离和时间下界排除预算必超的飞机。`candidate_screening` 包含：`reasons`（`RANGE_LOWER_BOUND` / `TIME_LOWER_BOUND` / `BOTH_LOWER_BOUNDS` / `SPEED_LIMIT` / `FLIGHT_RADIUS` 计数）、`nearest`（最近5架的航程/时间/半径是否通过，以及巡航速度下的时间和 `max_speed_mps`）、`range_ok_but_time_fail`（航程够但规划速度下时间不够的机号）、`excluded`（最多20条逐机依据，其余以 `excluded_omitted` 计数）、`assumed_transit_speed_mps` / `transit_speed_mps` / `work_speed_mps`（规划速度范围）和 `reachable_count` / `cost_excluded_count`。指定 `speed` 时，`max_speed` 低于该速度的候选自动以 `SPEED_LIMIT` 排除，不需要手工缩小候选范围。所有候选被筛掉时另有 `diagnosis`，给出最近候选、实际绑定约束（航程/时间/最大飞行半径/最大速度）和可执行建议。`alternatives` 给出按巡航速度规划失败、但指定不超过 `max_speed` 的全局速度后可完成对应作业的候选（`feasible_count`、`items`、`required_speed_mps`、`suggested_speed_mps`、`max_speed_mps`、`binding`）。单作业搜索在默认航高下不可行时另有 `altitude_alternatives`（`default_altitude_m`、`items[].altitude_m`、`total_range_m`、`total_time_s`、`battery_after`），给出可提高到的最低航高；提高航高后的识别能力需设备规格/实测确认。非固定搜索只保留一个候选时，工作量和时间按该候选的 FOV/速度重算；多候选共担仍是保守估计。将完整候选直接引用进来，后续分配使用成本产物的候选集。筛后超过64架返回 `CANDIDATE_LIMIT_EXCEEDED`，这是求解规模限制；不取前64架、最近64架或自定距离阈值替代全部候选。固定归属预算必超返回 `NO_FEASIBLE_ALLOCATION`。用户逐作业允许集合与筛后候选取交集，交集为空即报告该约束不可行。
- `NO_FEASIBLE_ALLOCATION` 时先读 `candidate_screening.diagnosis`，按实际绑定约束报告最近候选：只有 `RANGE_LOWER_BOUND` 才能说航程不足；`TIME_LOWER_BOUND` 说明是规划速度（默认逐机 `cruise_speed`）下时间不够，若最近候选航程通过且更高速度下时间足够，可指定不超过该机 `max_speed` 的速度后重算；`FLIGHT_RADIUS` 说明最远作业点超过 `max_flight_radius`，提高速度不能解决；`SPEED_LIMIT` 说明指定速度高于机型上限，应降低速度或换用支持该速度的机型。不能把所有候选概括为“航程不足”。若 `alternatives.feasible_count>0`，按“按巡航速度规划不可达、UAV-X 需不低于 Y m/s”说明；若 `altitude_alternatives.feasible_count>0`，按“默认航高不可行、提高到 Z m 下 UAV-X 可完成”说明；两者都为 0 时，说明当前候选池内没有机型能在其性能上限内完成。
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
- `summary.assignments` 同时给出每架执行机的 `battery_level`（原始电量）、`flight_remain`（原始剩余航程）、`range_budget/time_budget`（预留后预算）和 `estimated_battery_after`（预计剩余电量）；报告按同一机号直接读取这些值。
- 最终分区航程须满足输入航程预算且剩余电量大于20%。该估计只覆盖水平转场和覆盖/巡逻长度；不包含起降、搜索超时上界或跟踪占用，`whole_mission_budget_complete=false`，不能称全任务续航已验证。

`ALLOCATION_EVIDENCE_MISMATCH` 表示分配记录与成本或原始求解不对应；`ALTITUDE_INFEASIBLE` 表示错高超过限制；`BUDGET_EXCEEDED` 表示最终几何超过预算。失败时报告具体约束，不修改归属或放宽参数求成功。

```bash
uav_runtime/.venv/bin/python uav_runtime/run.py skills/trajectory-generation/scripts/mock_offline_map.py <子命令> …
```

## query-path：两点航线

```bash
uav_runtime/.venv/bin/python uav_runtime/run.py skills/trajectory-generation/scripts/mock_offline_map.py query-path \
  --start-lon <起点经度> --start-lat <起点纬度> --start-alt 0 \
  --end-lon <终点经度> --end-lat <终点纬度> --end-alt 0 --flight-alt 1.2
```

| 参数 | 必填 | 说明 |
|---|:---:|---|
| `--start-lon/--start-lat/--start-alt` | 单起点时是 | 起点经纬度和地面海拔 |
| `--starts-json` | 多起点时是 | `[{"uav_id":"UAV-001","longitude":…,"latitude":…,"altitude":0},…]`，与单起点参数不能同时用 |
| `--end-lon/--end-lat/--end-alt` | 是 | 终点经纬度和地面海拔 |
| `--flight-alt` | 是 | 正数相对航高，m；完整任务另核对机型和任务约束 |

返回 `go_waypoints`、`back_waypoints`、`one_way_distance`、`total_distance`（m）；按球面大圆计算水平距离并采样，不使用障碍地图。多起点时在 `results` 中按输入顺序逐项返回。需要复现采样点时可指定 `--seed`。

## query-area：区域航迹

```bash
uav_runtime/.venv/bin/python uav_runtime/run.py skills/trajectory-generation/scripts/mock_offline_map.py query-area \
  --center-lon <中心经度> --center-lat <中心纬度> --center-alt 0 \
  --shape rectangle --length <东西长> --width <南北宽> --mode coverage --flight-alt 1.2 --lane 2
```

| 参数 | 必填 | 说明 |
|---|:---:|---|
| `--center-lon/--center-lat/--center-alt` | 是 | 区域中心（地点库的 `region_center_*`，不是入口） |
| `--shape` | 是 | `rectangle` 或 `circle` |
| `--length`、`--width` / `--radius` | 按形状必填 | 矩形东西长、南北宽 / 圆形半径，m |
| `--mode` | 是 | `boundary`：边界巡逻闭合环；`coverage`：覆盖航迹（搜索航程估算） |
| `--flight-alt` | 是 | 正数相对航高，m |
| `--lane` | 否 | 扫描条带或边界采样间距，默认 2 m |
| `--start-lon/--start-lat` | 否 | 区域内的进入点，须成对给出 |

返回 `waypoints` 和 `path_length`（m）。`boundary` 另返回 `approach_waypoints`、`approach_length` 以及 `closed=true`、`boundary_complete=true`。

## 地图能力

本地离线入口没有 `query-map`。`semantic_map.py` 是外部语义地图入口，需要部署 `uav_runtime.map_provider` 及其地图资源；缺失时返回 `ENVIRONMENT_ERROR` 和具体组件名。只有环境检查确认外部语义地图可用时才使用该入口。

## 异常

| 状态或信息 | 含义与处理 |
|---|---|
| `INVALID_ARGUMENT` | 检查经纬度、航高、形状与尺寸、模式等参数，修正后最多重试一次 |
| `ENVIRONMENT_ERROR` | 外部语义地图未部署：报告缺失组件；需要离线预览时使用明确标记的本地入口 |
| `SEMANTIC_MAP_*`、`GLOBAL_ROUTE_*` | 外部地图的范围、障碍或可达性错误：保留具体原因，不用离线路径冒充避障结果 |
| `ERROR` | 报告错误并停止 |

## 注意事项

- 经纬度为 WGS84；高度、尺寸、距离单位为米。返回的航点和距离直接使用，不修改。
- `coverage` 的航迹和长度用于离线航程估算，不证明现场覆盖完成。
- 单项离线结果包含 `mock=true`、`map_used=false`、`submitted=false`、`validated_for_execution=false`；`closed` 和 `boundary_complete` 仅表示几何闭合及完整圆周/矩形边界。
- 按实际 `map_context` 判断地图与障碍是否已检查；当前本地入口为 `backend=mock`、`obstacle_checked=false`。面向用户说明“路线尚未核对现场障碍”，技术标记保留在结果文件中。
