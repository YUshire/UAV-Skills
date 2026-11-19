---
name: data-query
description: 用只读 SQL 查询资源库、状态库和地点库，保持记录字段原义；需要分配候选机时查询合格机及可用预算。用于资源和地点事实查询、相对位置基准查询及候选准备。
---

# 数据查询

## 可用函数

| 函数 | 脚本 | 表 |
|---|---|---|
| 资源库查询 | `query_uav_resource.py` | `uav_resource` |
| 状态库查询 | `query_uav_status.py` | `uav_status` |
| 地点库查询 | `query_location.py` | `geo_library` |

```bash
uav_runtime/.venv/bin/python uav_runtime/run.py skills/data-query/scripts/query_location.py \
  --sql "SELECT * FROM geo_library WHERE region_name = '教学楼' OR instr(';' || COALESCE(alias,'') || ';',';教学楼;') > 0"
```

- `--sql` 只能是一条 `SELECT` 或 `WITH … SELECT`，外层双引号、内层单引号；每个脚本只能查自己的表。
- 按需要选字段，不必 `SELECT *`（地点库除外，查地点时可取全部字段）。
- 互不依赖的查询放在同一轮并行发出。
- 地点一次查询同时匹配正式名和完整别名（不要先只查 `region_name`）；唯一命中直接使用坐标和登记区域，多个再结合已给地域、相邻步骤及会话上下文收窄，仍有多个才列出询问；简称未命中时结合地域检索，不用宽泛 `LIKE`+`LIMIT` 决定地点。其余所需事实从对应库补齐。

## 表字段

**uav_resource**（静态性能）

| 字段 | 说明 |
|---|---|
| `uav_id`、`alias` | 编号、别名 |
| `uav_model`、`uav_series` | 型号、系列 |
| `max_endurance`、`hover_endurance` | 最大续航、悬停续航，单位 min；时间预算只用 `max_endurance`，悬停未进预算 |
| `max_range` | 空载航程，m；只用于附近预筛，不是单机预算 |
| `max_flight_radius` | 最大飞行半径/控制半径，m；runtime 检查最远作业点 |
| `cruise_speed` | 巡航速度，m/s；未指定 `speed` 时的规划默认速度 |
| `max_speed` | 最大速度，m/s；只做上限，不作为规划速度 |
| `max_up_speed`、`max_down_speed` | 垂直速度，m/s；当前未进预算 |
| `max_altitude` | 最大飞行高度，m；航高预筛和多机错高上限 |
| `return_reserve` | 返航预留，%；预算取 `max(20, return_reserve)` |
| `max_wind_speed`、`emi_resistance_level` | 抗风、抗电磁干扰；当前只查询，不判可行 |
| `takeoff_mode`、`hover_accuracy`、`max_angular_velocity` | 起飞模式、悬停精度、最大角速度；当前只查询 |
| `max_operating_temp`、`min_operating_temp`、`min_takeoff_landing_distance` | 温度限制、最小起降距离；当前只查询/未用，不判可行 |

**uav_status**（状态记录）

本表没有 `alias`。数字机号（`307`、`307号机`、`UAV-307`）统一归一化为 `UAV-307`，按 `uav_id` 查，多机号用 `IN (...)`；非数字别名才先查 `uav_resource.alias` 拿 `uav_id`。

| 字段 | 说明 |
|---|---|
| `uav_id` | 编号 |
| `mission_status` | 空闲 / 执行中 / 离线 / 状态未知（由同步服务实时更新） |
| `longitude`、`latitude`、`altitude` | 登记的起飞点经纬度和场地海拔；规划用经纬度，`altitude` 当前不读 |
| `battery_level` | 电量记录，% |
| `health_score` | 健康度；预筛 >60，已包含在 `flight_remain` 里 |
| `payload_status` | 载荷：高清相机 / 红外相机 / 高清+红外 / 激光雷达；只做名称匹配 |
| `flight_remain` | 当前剩余可飞距离，m；已含健康度/电量/当前载荷，预算只扣返航预留 |
| `uav_weight`、`empty_weight`、`full_weight`、`load_coff` | 造库时推 `flight_remain`；runtime 不直接读 |
| `max_range_full`、`max_range_empty` | 满载/空载航程，m；造库时用于推 `flight_remain` |
| `load_range` | 机载载荷的最小**探测距离**，m；**不是航程**，不要用于距离预算 |
| `fov` | 视场角，deg；搜索航带 `lane=2×flight_alt×tan(fov/2)` |
| `camera_recognition_range`、`infrared_detection_range`、`lidar_detection_range` | 各类探测距离；只证明登记能力，不证明具体识别 |

**计算字段口径**：runtime 预算只读取 `flight_remain`、`battery_level`、`max_endurance`、`return_reserve`、`max_speed`、`max_altitude`、`max_flight_radius`、`fov` 和起飞点经纬度；预筛另读 `mission_status`、`health_score`、`payload_status`；`max_range` 只做附近预筛；`load_range` 是探测距离不是航程；`max_range_full/empty`、重量、`load_coff` 只在造库推 `flight_remain` 时使用；`flight_remain` 已含健康度/电量/当前载荷，预算只在其上扣返航预留，不再重复乘。`cruise_speed` 是未指定 `speed` 时的规划默认速度，`max_speed` 只做上限。

**geo_library**（地点）

| 字段 | 说明 |
|---|---|
| `region_name`、`alias` | 正式名称；别名以 `;` 分隔 |
| `longitude`、`latitude`、`altitude` | 地点入口坐标与海拔 |
| `region_shape`、`region_length_m`、`region_width_m`、`region_area_m2` | 登记的任务区域：形状、东西长、南北宽、面积；为空表示未登记区域 |
| `region_center_longitude`、`region_center_latitude` | 任务区域中心，与入口不同 |

“默认位置”是一个别名，查询写 `alias LIKE '%默认位置%'`。
仅方向+距离的指定单机请求，参照规则见 mission-planning；本库只提供起飞点记录。用户已指定该起飞点或会话已采用该基准时直接沿用；确需当前位置时查询可用实时来源，仍无法取得才询问。

## 候选机与可用预算

`scripts/prepare_candidates.py` 查询当前资源、状态和机队配置，输入可包含 `uav_ids`、`exclude_uavs`、`payload` 字符串数组，以及 `flight_alt`（默认1.2米）。还可给出 `center_lon`、`center_lat`（成对）和可选 `radius_m`、`job_radius_m`：未点名 `uav_ids` 时按起飞点到中心的距离做附近预筛，先用经纬度包围盒在 SQL 层只读取范围内的状态记录，再用球面距离收敛成圆形；省略 `radius_m` 时半径取 `max(max_range)/2 + job_radius_m`，区域作业把外接半径传给 `job_radius_m`；点名 `uav_ids` 时不套用该预筛。使用 AGENTS.md 中的 JSON 调用与结果引用方式：

```bash
uav_runtime/.venv/bin/python uav_runtime/run.py skills/data-query/scripts/prepare_candidates.py --input '{"uav_ids":["UAV-001","UAV-003"],"flight_alt":1.2}'
```

省略 `uav_ids` 表示查询全部合格机；候选数不表示实际出动数。保留原预筛条件：三处记录齐全、空闲、电量大于30%、健康度大于60、剩余航程有效、载荷符合明确要求、机型升限满足输入航高。点名候选未全部通过资源/状态预筛时返回 `CANDIDATE_EXCLUDED`，错误中带 `available_candidates`、`available_count`（当前筛选条件下可用的替代机摘要）和 `max_recorded_altitude`（记录中的最高升限）；需要全量替代机时去掉 `uav_ids`、带上作业中心重查，不能只报点名机不可达。无合格机返回 `NO_CANDIDATE`，同样带排除原因和记录中的最高升限。

给出作业中心时还会复用动作转换器检查该点是否在当前转换范围内，默认上限为距登记起飞点 10000 米；以 `summary.coordinate_filter.max_distance_m` 为准。超限原因在 `excluded[*].constraint="COORDINATE_RANGE"`，全部超限返回 `COORDINATE_RANGE_EXCEEDED`；点名机超限返回 `CANDIDATE_EXCLUDED`，不静默删掉点名机。此限制来自工具，不代表物理航程不足；若剩余候选又因预算失败，回复须合并两类排除证据，不能推断全机队物理无解。这里只检查已给中心，不认证整个区域或多作业组合均在范围内，最终仍由动作组装检查全部坐标；未给中心时不作这项预筛。

需要全机队或全量候选时用本入口（省略 `uav_ids`）；不要用 `SELECT ... LIMIT N` 代替全量。`truncated=false` 只表示本次 SQL 未被服务端截断，不代表查到了全表；用 SQL 统计时先 `COUNT(*)`，需要明细再分页。

任务规划时将完整 `candidates` 引用交给 `planning_routes.py operation=costs`，由其结合任务区域做预算预筛；候选多时不用手工截取机号。成本预筛同时检查 `range_budget`、`time_budget` 和 `max_flight_radius`（最远作业点不超过最大飞行半径），不是只看航程；速度不足时 `candidate_screening.alternatives` 会给出可在其 `max_speed` 内完成的替代机。

`payload` 数组各项必须同时满足，每项按状态记录字符串包含匹配，不表示多个名称任选其一。用户只要求红外能力时可用 `["红外"]` 匹配“红外相机”或“高清+红外”；不能把两种替代名称同时放入数组，变成额外的双重要求。具体载荷不明时先查询原记录，不靠探测距离推断。

完整结果在 artifact：`candidates` 包含机号、起飞原点、原始电量/航程/升限记录及 `range_budget`（米）、`time_budget`（秒）、`reserve_battery_percent`；`excluded` 列排除原因。返回的 summary 给出候选/排除数量、附近预筛 `spatial_filter` 和中心转换预筛 `coordinate_filter`，不回传全量明细。预算至少保留20%电量；资源记录 `return_reserve` 更高时按更高值预留，由工具计算，不把 `flight_remain` 或分钟制 `max_endurance` 直接改名作预算。本入口不查询任务地点、不计算路径、不决定归属。

## SQL 返回与异常

成功返回 `{"status":"OK","count":…,"truncated":…,"data":[…]}`，只读取 `data` 里实际返回的字段。

| 状态 | 处理 |
|---|---|
| `NOT_FOUND` | 核对全名、别名和已给地域后补查；仍无记录且缺任务必需事实时，询问具体地点或缺项 |
| `INVALID_ARGUMENT` | 对照上面的表名和字段修正 SQL，最多重试一次 |
| `DATABASE_ERROR`、`IO_ERROR`、`ERROR` | 报告数据库或文件错误，停止依赖它的步骤 |
| `truncated=true` | 结果超过 100 行：收窄条件或分页，不把第一批当作全部 |

## 注意事项

- 经名称、地域和上下文筛选后仍有多个实际候选时，一次列出名称或区别供用户选择。用户已给完整坐标和区域时可直接交空间计算；只有依赖库中事实的缺项才需要补问。
- 不做表结构枚举（PRAGMA、sqlite_master），可用字段以本文件为准；禁止写入。
- 状态与资源通过 `uav_id` 关联；查电量、任务状态这类动态信息时每次重新查询，不复用对话里的旧结果。
- 状态库的位置是起飞点记录，不是飞行中的实时位置；运行中的状态以中央任务 `get` 为准。
- 载荷类型和探测距离是两种约束，不能互相推导。
