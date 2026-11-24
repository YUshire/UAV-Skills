---
name: data-query
description: 【唯一查库入口】负责：只读 SQL 查询无人机资源库、状态库、地点库，以及筛选合格候选机与预算（prepare_candidates）。不负责：空间偏移计算、航迹规划与任务分配。
---

# 数据查询（唯一数据库权威源）

## SOP 路由
- 查机况/电量/载荷/起飞点/地点/常见静态性能 → 直接按本页 SQL 查询。
- 查型号/悬停/垂直速度/抗风温湿度/探测距离等扩展字段 → 仅此时读 [table-schema.md](references/table-schema.md)。
- 排查候选筛选规则、预算折减或转换预筛细节 → 仅此时读 [candidates.md](references/candidates.md)。

## 1. 三张只读表与脚本映射

| 查询对象 | 脚本路径（接 `run.py` 后） | 表名 | 常用字段 |
|---|---|---|---|
| 静态资源 | `skills/data-query/scripts/query_uav_resource.py` | `uav_resource` | `uav_id,alias,max_endurance(min),max_range(m),max_flight_radius(m),cruise_speed,max_speed(m/s),max_altitude(m),return_reserve(%)` |
| 机况状态 | `skills/data-query/scripts/query_uav_status.py` | `uav_status` | `uav_id,mission_status(空闲/执行中/离线/状态未知),longitude,latitude,altitude,battery_level(%),health_score,payload_status,flight_remain(m),fov(deg)` |
| 地点库 | `skills/data-query/scripts/query_location.py` | `geo_library` | `region_name,alias(;分隔),longitude,latitude,altitude,region_shape,region_length_m,region_width_m,region_area_m2,region_center_longitude,region_center_latitude` |

### 标准 SQL 模板（互不依赖查询同轮并行）
```bash
# 地点：一次同时匹配正式名和完整别名（可用 SELECT *）
uav_runtime/.venv/bin/python uav_runtime/run.py skills/data-query/scripts/query_location.py \
  --sql "SELECT * FROM geo_library WHERE region_name = '教学楼' OR instr(';' || COALESCE(alias,'') || ';',';教学楼;') > 0"

# 状态/资源：数字机号归一化 UAV-XXX（73/73号机/UAV-73 → UAV-073），多机用 IN (...)
uav_runtime/.venv/bin/python uav_runtime/run.py skills/data-query/scripts/query_uav_status.py \
  --sql "SELECT uav_id,mission_status,longitude,latitude,battery_level,health_score,payload_status,flight_remain,fov FROM uav_status WHERE uav_id IN ('UAV-073','UAV-258')"
```

- `--sql` 仅允许单条 `SELECT` / `WITH … SELECT`（外层双引号、内层单引号），每个脚本只能查自己的表。`uav_status` **没有 `alias` 和 `max_flight_radius`**。
- 禁止 `PRAGMA`/`sqlite_master` 枚举，禁止写入，禁止用宽泛 `LIKE`+`LIMIT` 决定地点或拿 `LIMIT` 样本冒充全机队。
- 地点多命中先结合地域/上下文收窄，仍有多个才列出询问；简称未命中再结合地域定向查；“默认位置”查 `alias LIKE '%默认位置%'`。

## 2. 合格候选机与预算筛选（`prepare_candidates.py`）
```bash
# 点名机筛选；查全量周边替代机时去掉 uav_ids 并传作业中心 center_lon/center_lat
uav_runtime/.venv/bin/python uav_runtime/run.py skills/data-query/scripts/prepare_candidates.py \
  --input '{"uav_ids":["UAV-001","UAV-003"],"flight_alt":1.2}'
```
- 可选字段：`uav_ids`、`exclude_uavs`、`payload`（字符串数组，全部同时满足，如 `["红外"]`）、`flight_alt`（默认 1.2）、`center_lon`/`center_lat`（成对，未点名时做附近与坐标转换预筛）、`job_radius_m`。
- 预筛自动核对：空闲、电量 >30%、健康度 >60、剩余航程有效、载荷与升限满足；预算由工具扣除返航预留（至少 20%），不手工二次折减。点名机不满足返回 `CANDIDATE_EXCLUDED`（附 `available_candidates` 与 `max_recorded_altitude`）。
- **直接复用 `summary.candidates_top`（禁止拆读 `candidates-*.json`）**：返回摘要已按可达性与距离排序给出前 15 架精简信息（含 `uav_id`/`payload`/`max_endurance_min`/`dist_m`/`max_flight_radius_m`/`range_budget_m`/`time_budget_s`/`reachable_lb`）；给出周边替代机时**仅推荐 `reachable_lb=true` 的机号**，严禁 `read_file` 或 `python -c` 解析候选大产物。

## 3. 字段语义与异常
- **语义铁律**：状态库坐标是**登记起飞点**（不是飞行实时位置）；电量/健康/剩余航程是快照记录（每次重查不复用旧对话）；`max_speed` 是上限而非默认巡航速度；地点入口坐标 ≠ 登记区域中心；载荷名称 ≠ 探测距离 ≠ 实际识别能力。
- **异常处理**：`NOT_FOUND` 核对全名/别名/地域补查 1 次，仍无再问；`INVALID_ARGUMENT` 对照本页字段修正重试 1 次；`truncated=true` 收窄或分页；`DATABASE_ERROR`/`ERROR` 停止依赖步骤。
