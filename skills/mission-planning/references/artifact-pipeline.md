# 普通任务规划 SOP（`--pipeline` 一页闭环）

> 仅用于普通搜索（含无目标探索、两机分区共担）、边界巡逻、同机多步混合及可行性评估。含扫描/录像/拍照/观察/切换模式/条件分支/跨机先后/动态发现位置/纯飞行混合时，**不读本页，改读 [task-graphs.md](task-graphs.md)**。

## 4 步标准执行动作序列（SOP）

1. **查事实（1 轮并行）**：
   - 运行模式由 `AGENTS.md` 静态开关决定，禁止调用 `self_check.py` 或预先探测环境。
   - 并行用 `data-query` 查所需地点、点名机状态/资源；若指令含“东南/正北 X 米”等相对偏移，读 `spatial-compute` 并调用 `calc_offset.py`；若为同机多步混合，调 `check_task.py`。
   - **无需读取** `flight-control/SKILL.md`、`mission-orchestration/SKILL.md` 或 `trajectory-generation/SKILL.md`。
2. **一次串联规划（`--pipeline`）**：
   - 将 6 步串联 JSON 写入 `artifacts/requests/in_<唯一名>.json` 并执行：
     `uav_runtime/.venv/bin/python uav_runtime/run.py --pipeline --input-file artifacts/requests/in_<唯一名>.json`
   - 若某步失败，工具已返回 `failed_step`、`last_ok_summary` 与诊断字段，**禁止拆读 `artifacts/`**，直接读 [diagnostics.md](diagnostics.md) 按表处置。
3. **目标校验与交付落盘（1 轮并行）**：
   - 带搜索目标时，读 `target-detection/SKILL.md` 并**仅对实际承担带目标搜索的执行机**调用 `validate_targets.py`（`search_id` 用 `summary.task_key`）。
   - **仅当 `assemble_plan` 校验通过（`artifact_created=true`）**且非单纯可行性咨询时，按 [plan-delivery.md](plan-delivery.md) 将交付请求 JSON 写入 `artifacts/requests/delivery_<task_key>.json`；若 `assemble_plan` 整体失败，禁止写完整交付请求 JSON，严禁修改上游 `build` 产物跑子集。
4. **按模板交付**：
   - 直接使用 `summary`（已含 `task_key`、`jobs`、`participants`、`speed`、`takeoff_to_jobs_m`、`show_actions`、`budget_scope`、`max_distance_m`），按 [plan-delivery.md](plan-delivery.md) 输出日常中文回复；当前 `PLANNING_ONLY` 模式**不读** `online-submission.md`、**不调** `mission_client.py`（仅 `ONLINE_EXEC` 模式再进入中央下发）。
   - **必展动作序列**：只要 `assemble_plan` 校验通过并产出可下发动作序列（`summary.participants[*].actions` 存在且 `summary.show_actions` 不为 `false`），最终回复**必须**用合法 ` ```json ` 代码块逐机完整展示动作序列，严禁遗漏或仅用文字概括。

## `--pipeline` 完整模板（标准 6 步合法 JSON）

```json
{
  "steps": [
    {"id":"fleet","script":"skills/data-query/scripts/prepare_candidates.py","input":{"uav_ids":["UAV-016"],"flight_alt":1.2}},
    {"id":"regions","script":"skills/spatial-compute/scripts/resolve_jobs.py","input":{"jobs":[{"job_id":"J1","name":"示例圆形区域","center_lon":121.442057,"center_lat":25.239462,"shape":"circle","radius":10,"task_type":"search"}]}},
    {"id":"costs","script":"skills/trajectory-generation/scripts/planning_routes.py","input":{"operation":"costs","candidates":{"ref_step":"fleet","select":"/data/candidates"},"jobs":{"ref_step":"regions","select":"/data/jobs"},"flight_alt":1.2,"ordered":false}},
    {"id":"allocation","script":"skills/mission-orchestration/scripts/allocate.py","input":{"candidates":{"ref_step":"costs","select":"/data/candidates"},"jobs":{"ref_step":"costs","select":"/data/jobs"},"costs":{"ref_step":"costs","select":"/data/costs"},"constraints":{"ref_step":"costs","select":"/data/constraints"}}},
    {"id":"routes","script":"skills/trajectory-generation/scripts/planning_routes.py","input":{"operation":"build","cost_estimate":{"ref_step":"costs"},"allocation":{"ref_step":"allocation"}}},
    {"id":"actions","script":"skills/flight-control/scripts/assemble_plan.py","input":{"plan":{"ref_step":"routes"}}}
  ]
}
```

### 关键输入规则
- **`fleet`（`prepare_candidates.py`）**：点名机写 `uav_ids`；未点名（任意机/派 N 架）**省略 `uav_ids`**，并传作业中心 `center_lon`/`center_lat`（及可选 `job_radius_m`）做附近预筛；有明确载荷要求传 `payload:["红外"]`（普通目视不传）。
- **`regions`（`resolve_jobs.py`）**：
  - 允许字段仅 `job_id, name, place, center_lon, center_lat, shape, radius, length, width, task_type, laps, search_timeout, uav_id`。用户给了形状/尺寸就覆盖登记区；圆形必填 `radius`；矩形填 `shape:"rectangle", length:<东西向米>, width:<南北向米>`；巡逻填 `task_type:"patrol", laps:<圈数>`。
  - **【必看】按地名解析时**：凡 `jobs` 中任一项写了 `"place":"<地点名>"`，`regions.input` **必须同时传 `"places": <query_location 返回的 data 原数组>`**（全为经纬度作业时省略 `places`）。
- **`costs`（`planning_routes.py`）**：同机“先…再…”设 `"ordered": true`；**结束动作与单/往返**：**未指定是否返航时省略 `return_to_home`（默认 `true`，按往返计算，结束后返航+`land`）**；仅当用户**明确要求盘旋/悬停/待命/不返航**时传 `"return_to_home": false`（只算单程，结束后自动原地 `hover`、不加返航与 `land`）。用户指定全局速度时传 `speed`（先核对参与机 `max_speed`，超限不静默改速）；**不向 `costs` 传 `required_drones`/`allowed_assignments`**。
- **`allocation`（`allocate.py`）**：
  - 两机共担/分区搜索某作业（“分成两块，各搜一块”或“派两架搜同一区域”）：写成 **1 个作业**，在 `allocation.input` 增加 `"extra_constraints":{"required_drones":2}`（点名机时加 `"allowed_assignments":{"J1":["UAV-A","UAV-B"]}`），不手拆作业，分区由 `build` 自动完成；
  - 无额外约束时省略 `extra_constraints`；多区共用同一批候选机但各区限定不同机号时，在 `extra_constraints.allowed_assignments` 逐作业列出各自允许机号。

## 意图映射、默认值与能力拦截
- **默认值与自动错高**：航高 `1.2 m`（须 `> 0.3 m` 且 ≤ 机型升限；**多机任务中 `build` 自动按 `+0.3 m` 阶梯错高如 `1.2/1.5/1.8 m` 防碰撞，属正常设计，勿拆产物排查**）；巡逻 `1` 圈；搜索时限 `1200 s`（20 分钟）；速度用逐机 `cruise_speed`（`max_speed` 仅上限）；**结束动作默认返航降落、按往返计算（省略 `return_to_home` 即默认 `true`）**，仅明确要求盘旋/悬停/待命/不返航时设 `return_to_home: false`。
- **日常表达与弓字形归类**：
  - “看看/扫一遍/查看某区域”且无上下文目标 → 无目标探索（`task_type:"search"`，不追问目标、不调 `validate_targets`）；
  - “周围 N 米扫一遍 / 巡两圈” → 以基准中心为圆心、半径 `N` 米圆形（明确说距边界外扩时保留外扩语义）；
  - **弓字形区域覆盖**：凡表达全面覆盖/往复扫描的近似说法（如弓字形、拉网式、筛网式、梳状等类似表述），均按语义统一归类为**弓字形（平行往复）区域覆盖**，走常规 `--pipeline` 的 `task_type:"search"` 规划并输出动作 JSON（同时如实说明实际执行框范围限制）。
- **超界能力直接拦截（禁止静默替代、禁止输出动作 JSON）**：
  - 明确要求**逐圈环形推进 / 同心圆逐圈覆盖 / 螺旋式由外向内或由内向外收拢**、**边巡边持续报进度**、**禁限飞区按批准航线绕行**、**遇障自动升高规避后再恢复贴边**、**全程通信链路保障**、**跨趟周期/定时循环巡逻**、**实体去重计数** → 直接按 [plan-delivery.md](plan-delivery.md) **模板 B** 拦截告知“任务无法下发（能力不足）”并反问用户是否调整，**绝不**用普通探索/巡逻偷换概念。
  - 参数非法或自相冲突（负速度、航高超升限等）→ 不自改原值，一次问清冲突项。
