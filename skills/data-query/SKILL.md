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
  --sql "SELECT * FROM geo_library WHERE region_name = '教学楼' OR alias LIKE '%教学楼%' LIMIT 3"
```

- `--sql` 只能是一条 `SELECT` 或 `WITH … SELECT`，外层双引号、内层单引号；每个脚本只能查自己的表。
- 按需要选字段，不必 `SELECT *`（地点库除外，查地点时可取全部字段）。
- 互不依赖的查询放在同一轮并行发出。
- 地点先按全名和完整别名匹配，简称再结合用户已给地域、相邻步骤及会话上下文收窄。唯一匹配后直接使用坐标和登记区域；其余所需事实从对应库补齐。宽泛查询返回多行时先筛选相关记录。

## 表字段

**uav_resource**（静态性能）

| 字段 | 说明 |
|---|---|
| `uav_id`、`alias` | 编号、别名 |
| `uav_model`、`uav_series` | 型号、系列 |
| `max_endurance`、`hover_endurance` | 最大续航、悬停续航，单位 min |
| `max_range`、`max_flight_radius` | 最大航程、最大飞行半径，单位 m |
| `cruise_speed`、`max_speed`、`max_up_speed`、`max_down_speed` | 速度，单位 m/s |
| `max_altitude` | 最大飞行高度，单位 m |
| `max_wind_speed`、`emi_resistance_level` | 抗风、抗电磁干扰等级 |
| `takeoff_mode`、`hover_accuracy`、`max_angular_velocity` | 起飞模式、悬停精度、最大角速度 |

**uav_status**（状态记录）

本表没有 `alias`。用户给“107号机”这类别名时，从 `uav_resource.alias` 查得 `uav_id`，再以该编号查询状态；已明确 `UAV-107` 时直接按编号查询。

| 字段 | 说明 |
|---|---|
| `uav_id` | 编号 |
| `mission_status` | 空闲 / 执行中 / 离线 / 状态未知（由同步服务实时更新） |
| `longitude`、`latitude`、`altitude` | 记录的起飞点位置，不随飞行更新 |
| `battery_level` | 电量记录，% |
| `health_score` | 健康度 |
| `payload_status` | 载荷：高清相机 / 红外相机 / 高清+红外 / 激光雷达 |
| `flight_remain` | 剩余可飞行距离，m |
| `max_range_full`、`max_range_empty`、`load_range` | 满载航程、空载航程、载荷航程，m |
| `fov`、`camera_recognition_range`、`infrared_detection_range`、`lidar_detection_range` | 视场角、各类探测距离 |

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

省略 `uav_ids` 表示查询全部合格机；候选数不表示实际出动数。保留原预筛条件：三处记录齐全、空闲、电量大于30%、健康度大于60、剩余航程有效、载荷符合明确要求、机型升限满足输入航高。点名候选未全部合格返回 `CANDIDATE_EXCLUDED`，无合格机返回 `NO_CANDIDATE`。

需要全机队或全量候选时用本入口（省略 `uav_ids`）；不要用 `SELECT ... LIMIT N` 代替全量。`truncated=false` 只表示本次 SQL 未被服务端截断，不代表查到了全表；用 SQL 统计时先 `COUNT(*)`，需要明细再分页。

任务规划时将完整 `candidates` 引用交给 `planning_routes.py operation=costs`，由其结合任务区域做预算预筛；候选多时不用手工截取机号。

`payload` 数组各项必须同时满足，每项按状态记录字符串包含匹配，不表示多个名称任选其一。用户只要求红外能力时可用 `["红外"]` 匹配“红外相机”或“高清+红外”；不能把两种替代名称同时放入数组，变成额外的双重要求。具体载荷不明时先查询原记录，不靠探测距离推断。

完整结果在 artifact：`candidates` 包含机号、起飞原点、原始电量/航程/升限记录及 `range_budget`（米）、`time_budget`（秒）；`excluded` 列排除原因。返回的 summary 只给 `candidate_count`、`excluded_count`、`bbox_input_count`、`circle_excluded_count`、`spatial_filter` 和最多 5 个 `sample_uav_ids`，不回传全量明细。预算保留20%电量，由工具计算，不把 `flight_remain` 或分钟制 `max_endurance` 直接改名作预算。本入口不查询任务地点、不计算路径、不决定归属。

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
