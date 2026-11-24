# 复杂任务图规划 SOP（`plan_task_graph.py` 一页闭环）

> 适用：独立接口步骤（`scan` 扫描战壕/道路、`record_start/stop`、`photo`、`set_mode` 红外/高清、`track`、`observe`、`hover`、`wait`、`report`、`transit` 纯飞行途经点）、条件分支（`found`/`not_found`/`timeout`）、发现后动态位置、或跨机先后依赖。**本入口只规划不下发；无需读取 `artifact-pipeline.md` 或 `combined-tasks.md`。**

## 4 步标准执行动作序列（SOP）

1. **查事实（1 轮并行）**：
   - 运行模式由 `AGENTS.md` 静态开关决定，禁止调用 `self_check.py` 或预先探测环境。
   - 并行用 `data-query` 查地点、点名机状态/资源（未点名时查可用候选或由图规划器自动选机）；含相对偏移时读 `spatial-compute` 调 `calc_offset.py`。
2. **构图并一次规划**：
   - 将任务图 JSON 写入 `artifacts/requests/tg_<唯一名>.json`，执行：
     `uav_runtime/.venv/bin/python uav_runtime/run.py skills/mission-planning/scripts/plan_task_graph.py --input-file artifacts/requests/tg_<唯一名>.json`
   - **严禁**再重复跑普通 `--pipeline` 或多次试探 `plan_task_graph`。
   - **纯飞行（`transit`）与超预算硬截断**：仅当含 `transit` 且需全航段距离估算时，**同轮并行**调 `offline-preview.md` 的 `query-path`/`query-area` 与 1 次 `prepare_candidates`；**一旦预览总航程已超 `range_budget`，立刻止损按超预算交付，禁止手算大圆距离、禁止反事实假设（不返航/提速/满电）、禁止再套跑 `--pipeline`**。
   - 若工具返回 `INFEASIBLE` / `BLOCKED` / `TIME_RESERVATION`，直接读 [diagnostics.md](diagnostics.md) 按表处置（如 `retained_uav_ids` 非空或需查周边替代机，调 1 次 `prepare_candidates`）。
3. **目标校验（仅带检测目标时）**：
   - 读 `target-detection/SKILL.md`，用 `summary.task_keys`（或对应分支 `summary.scenarios[*].task_key`）作 `search_id`，仅对带检测目标的搜索执行机调 `validate_targets.py`。
   - **扫描对象（战壕/道路）放在 `scan` 节点，绝不塞进 `targets`。**
4. **按 `summary` 交付（无需拆产物）**：
   - `summary` 已包含 `planning_status`、`show_actions`、`task_keys`、`budget_scope`、`reply` 以及去重后的 `scenarios`（含各分支 `task_key`、`assignments`、`reserved_airborne_time_s`、`takeoff_to_jobs_m`、`violations`、`pending`）。
   - 按 [plan-delivery.md](plan-delivery.md) 用日常中文逐项说明执行机、区域/对象、先后/并行、触发条件、时长（缺则标待补）、结束动作、全部 `violations`/`pending` 告警及未下发/未验证边界；保留原图请求文件作为意图记录。

## 输入 Schema 速查（一次写对，禁止试错）

顶层字段：`nodes`（必填，1..64）、`edges`（最多 256）、可选 `flight_alt`（默认 1.2，须 `> 0.3`）、`speed`、`search_timeout`（默认 1200）、`uav_ids`、`exclude_uavs`、`payload`、`constraints`、`solver_options`、`max_duration_s`、`observed_events`、`runtime_bindings`。

- **`constraints` 精确类型（严禁写错）**：
  - `fixed_assignments`：`{"节点id": "UAV-XXX"}`（单机固定字典）
  - `allowed_assignments`：`{"节点id": ["UAV-A", "UAV-B"]}`（节点允许机号数组字典）
  - `single_owner_jobs`：`["节点id"]`（禁止拆分的节点数组）
  - `required_drones`：**全局正整数 `1..64`（绝非字典！）**，表示本次实际出动总架数；分组双机共担用 `allowed_assignments` + 全局 `required_drones` 组合实现。

| 节点字段 | 严格契约 |
|---|---|
| `id` | 唯一编号，字母开头，仅 `[A-Za-z0-9_-]` |
| `action` | 区域动作：`search`、`patrol`；接口动作：`scan`、`track`、`observe`、`record_start`、`record_stop`、`photo`、`set_mode`、`hover`、`wait`、`report`、`transit`、`return`、`land` |
| `uav_id` | 仅单架指定机时填。**多机共担或自动选机时省略节点层 `uav_id`**，改在顶层 `constraints` 写 `allowed_assignments` 与全局整数 `required_drones` |
| `job` | **仅 `search`/`patrol` 必填**（`place` 或 `center_lon/center_lat` + `shape` + 尺寸 + 可选 `laps`/`search_timeout`/`name`；发现后动态区域用 `"center_from":{"from_step":"S1","field":"target_position"}` 且须受 `found` 约束）。**严禁在 `job` 内写 `job_id`/`uav_id`/`task_type`** |
| `params` | **仅允许 6 个键**：`duration_s`（正秒数）、`mode`、`target`、`targets`、`description`、`until_step`（其他键报错）。`transit`/`report`/`scan`/`track`/`observe` **不写 `job`**；`track` 的 `params.target` 填目标名称字符串（如 `"发现目标"`），**严禁**写 `{"from_step":...,"field":"target"}`（动态引用唯一合法格式为 `{"from_step":"S1","field":"target_position"}` 且仅限 `found` 分支） |
| `join` | 默认 `all`（全部前置满足）；`any`（任一满足）。一般**省略显式 `return`/`land`**（工具自动为全部分支补返航降落）；若显式写了汇合节点且同时依赖互斥/条件分支，须设 `join:"any"` |

- **依赖边**：`{"from":"S1","to":"T1","on":"completed|found|not_found|timeout"}`（省略 `on` 默认 `completed`；后三者仅来自 `search` 且互斥）。无界循环（“直到找到”）须先说明需有限终止条件或展开为有界节点。
- **模糊/动态语义默认建模约定（禁止发散与反复改图）**：
  - **未点名机号**：用户仅说“找架续航较好/空闲的无人机”而未指定机号时，**省略顶层 `uav_ids` 与节点 `uav_id`**，让规划器自动在全部可达机中择优（避免手选单机漏掉默认航高即可行的更近机）；
  - **载荷保真**：用户未明确指定载荷（如仅说“扫描战壕”未提激光雷达）时，严禁擅自加 `payload` 过滤；
  - **区域默认**：“某地周围 N 米内”默认以该地登记区域中心为圆心、半径 `N` 米圆形（仅明确说“距边界外扩”时才外扩）；
  - “多目标分头找”直接按给定区域与机组各建 1 个 `search` 节点（未把目标逐一绑死到特定区域时，各搜索节点均挂全部待找目标列表）；
  - “确认找到后由能接手的那架跟着，其余继续搜”直接建省略 `uav_id` 的 `track` 节点并用 `on:"found"` 连接即可，未明确的跟踪执行机/时长交由工具自动标 `pending`（`NEEDS_EXECUTOR`/`NEEDS_DURATION`/`RUNTIME_TRAJECTORY`），**禁止**脑补续搜节点（如 `S1b/S2b`）或在思考中反复推翻建模方案。

## 示例（多机分组共担 + 条件跟踪 + `constraints` 精确写法）
```json
{
  "uav_ids": ["UAV-343","UAV-382","UAV-292","UAV-231"],
  "flight_alt": 1.2,
  "constraints": {
    "allowed_assignments": {"S1": ["UAV-343","UAV-382"], "S2": ["UAV-292","UAV-231"]},
    "required_drones": 4
  },
  "nodes": [
    {"id":"S1","action":"search","job":{"name":"区域A","center_lon":121.628265,"center_lat":23.998125,"shape":"rectangle","length":12,"width":8},"params":{"targets":["穿蓝色上衣的人","穿橙色上衣的人"]}},
    {"id":"S2","action":"search","job":{"name":"区域B","center_lon":119.613196,"center_lat":23.5693,"shape":"rectangle","length":20,"width":10},"params":{"targets":["穿蓝色上衣的人","穿橙色上衣的人"]}},
    {"id":"T1","action":"track","params":{"target":"发现目标","description":"确认找到人后由能接手的那架跟踪，其余继续搜"}},
    {"id":"T2","action":"track","params":{"target":"发现目标","description":"确认找到人后由能接手的那架跟踪，其余继续搜"}}
  ],
  "edges": [{"from":"S1","to":"T1","on":"found"},{"from":"S2","to":"T2","on":"found"}]
}
```
> 常见单节点写法速查（均不写 `job`）：
> - **扫描（`scan`）**：单区域扫描直接用 `S1(action:"search", job:{"place":"..."})` → `SC1(action:"scan", params:{"targets":["战壕"],"description":"扫描战壕并采集数据"})`（`completed` 边连接，勿额外加 `transit`，未指定载荷不传 `payload`）。
> - **纯飞行途经点（`transit`）**：`{"id":"M1","action":"transit","uav_id":"UAV-107","params":{"target":"屏东万金机械化步兵营区","description":"飞往(120.4875,22.674313)"}}`。

## 结果判读与执行边界
- 外层 `status=OK` 仅代表正常返回；以 `summary.planning_status` 为准：`PLANNED` / `CONDITIONAL` / `NEEDS_DATA` / `INFEASIBLE` / `BLOCKED`。
- `reserved_airborne_time_s` 已包含起降、完整搜索时限、已知等待与返航（勿与单节点阶段耗时混淆）；出现 `TIME_RESERVATION` 告警必须如实告知，但不能断言物理无解（可建议缩短搜索时限）。
- 严格同高冲突（多机自动错高 +0.3m）与扫描中实时同时检测/中断跟踪均如实说明尚未满足或未验证；严禁把条件图或几何前段拼进旧 `queue_batch` 下发；即使有分支不可行，回复仍须保留全部节点、触发条件、时长与结束动作。
