# 候选机准备与预算

仅准备分配候选及预算、或排查候选筛选时读；单独查询一架机电量不需要本页。候选输入与事实沿用本次任务，不先用多轮 SQL 手算同一预算。

`scripts/prepare_candidates.py` 查询当前资源、状态和机队配置，输入可包含 `uav_ids`、`exclude_uavs`、`payload` 字符串数组，以及 `flight_alt`（默认1.2米）。还可给出 `center_lon`、`center_lat`（成对）和可选 `radius_m`、`job_radius_m`：未点名 `uav_ids` 时按起飞点到中心的距离做附近预筛，先用经纬度包围盒在 SQL 层只读取范围内的状态记录，再用球面距离收敛成圆形；省略 `radius_m` 时半径取 `max(max_range)/2 + job_radius_m`，区域作业把外接半径传给 `job_radius_m`；点名 `uav_ids` 时不套用该预筛。使用 AGENTS.md 中的 JSON 调用与结果引用方式：

```bash
uav_runtime/.venv/bin/python uav_runtime/run.py skills/data-query/scripts/prepare_candidates.py --input '{"uav_ids":["UAV-001","UAV-003"],"flight_alt":1.2}'
```

省略 `uav_ids` 表示查询全部合格机；候选数不表示实际出动数。保留原预筛条件：三处记录齐全、空闲、电量大于30%、健康度大于60、剩余航程有效、载荷符合明确要求、机型升限满足输入航高。点名候选未全部通过资源/状态预筛时返回 `CANDIDATE_EXCLUDED`，错误中带 `available_candidates`、`available_count`（当前筛选条件下可用的替代机摘要）和 `max_recorded_altitude`（记录中的最高升限）；需要全量替代机时去掉 `uav_ids`、带上作业中心重查，不能只报点名机不可达。无合格机返回 `NO_CANDIDATE`，同样带排除原因和记录中的最高升限。

给出作业中心时还会复用动作转换器检查该点是否在当前转换范围内，上限动态读取转换器当前默认值；说明本次限制时采用工具返回的 `summary.coordinate_filter.max_distance_m`，失败时读 `coordinate_filter.max_distance_m`，不记忆或写死某个公里数。超限原因在 `excluded[*].constraint="COORDINATE_RANGE"`，全部超限返回 `COORDINATE_RANGE_EXCEEDED`；点名机超限返回 `CANDIDATE_EXCLUDED`，不静默删掉点名机。此限制来自工具，不代表物理航程不足；若剩余候选又因预算失败，回复须合并两类排除证据，不能推断全机队物理无解。这里只检查已给中心，不认证整个区域或多作业组合均在范围内，最终仍由动作组装检查全部坐标；未给中心时不作这项预筛。

需要全机队或全量候选时用本入口（省略 `uav_ids`）；不要用 `SELECT ... LIMIT N` 代替全量。`truncated=false` 只表示本次 SQL 未被服务端截断，不代表查到了全表；用 SQL 统计时先 `COUNT(*)`，需要明细再分页。

任务规划时将完整 `candidates` 引用交给 `planning_routes.py operation=costs`，由其结合任务区域做预算预筛；候选多时不用手工截取机号。成本预筛同时检查 `range_budget`、`time_budget` 和 `max_flight_radius`（最远作业点不超过最大飞行半径），不是只看航程；速度不足时 `candidate_screening.alternatives` 会给出可在其 `max_speed` 内完成的替代机。

`payload` 数组各项必须同时满足，每项按状态记录字符串包含匹配，不表示多个名称任选其一。用户只要求红外能力时可用 `["红外"]` 匹配“红外相机”或“高清+红外”；不能把两种替代名称同时放入数组，变成额外的双重要求。具体载荷不明时先查询原记录，不靠探测距离推断。

完整结果在 artifact：`candidates` 包含机号、起飞原点、原始电量/航程/升限记录及 `range_budget`（米）、`time_budget`（秒）、`reserve_battery_percent`；`excluded` 列排除原因。返回的 summary 给出候选/排除数量、附近预筛 `spatial_filter` 和中心转换预筛 `coordinate_filter`，不回传全量明细。预算至少保留20%电量；资源记录 `return_reserve` 更高时按更高值预留，由工具计算，不把 `flight_remain` 或分钟制 `max_endurance` 直接改名作预算。本入口不查询任务地点、不计算路径、不决定归属。

## 字段与预算口径

runtime 预算只读取 `flight_remain`、`battery_level`、`max_endurance`、`return_reserve`、`max_speed`、`max_altitude`、`max_flight_radius`、`fov` 和起飞点经纬度；预筛另读 `mission_status`、`health_score`、`payload_status`；`max_range` 只做附近预筛；`load_range` 是探测距离不是航程；`max_range_full/empty`、重量、`load_coff` 只在造库推 `flight_remain` 时使用；`flight_remain` 已含健康度/电量/当前载荷，预算只在其上扣返航预留，不再重复乘。`cruise_speed` 是未指定 `speed` 时的规划默认速度，`max_speed` 只做上限。
