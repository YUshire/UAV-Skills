---
name: data-query
description: 用只读 SQL 查询资源库、状态库和地点库，保持记录字段原义；需要分配候选机时查询合格机及可用预算。用于资源和地点事实查询、相对位置基准查询及候选准备。
---

# 数据查询

| 当前请求 | 读取与处理 |
|---|---|
| 查电量、机况、地点、载荷记录或登记性能 | 按本页只读查询与常用字段处理 |
| 所需字段不在本页，如型号/环境性能/重量/探测距离 | 仅此时读 [table-schema.md](references/table-schema.md) |
| 为执行任务准备合格候选与可用预算 | 读 [candidates.md](references/candidates.md)，用 prepare_candidates，不手工截取候选 |

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

## 常用字段与归属

先核对表再选字段，不能把静态性能字段查到状态表。`uav_status` **没有 alias，也没有 max_flight_radius**。

| 表 | 常用字段及含义 |
|---|---|
| `uav_resource` | `uav_id,alias`；`max_endurance` 最大续航（min）；`max_range` 空载航程（m，仅附近预筛）；`max_flight_radius` 飞行半径（m）；`cruise_speed` 默认巡航速度、`max_speed` 速度上限（m/s）；`max_altitude` 升限（m）；`return_reserve` 返航预留（%） |
| `uav_status` | `uav_id`；`mission_status` 空闲/执行中/离线/状态未知；`longitude,latitude,altitude` 登记起飞点与场地海拔；`battery_level` 电量（%）；`health_score` 健康度；`payload_status` 载荷名称；`flight_remain` 剩余可飞距离（m，已含健康度/电量/载荷）；`fov` 视场角（deg） |

数字机号统一归一化为标准编号：`73`、`73号机`、`UAV-73` 都按 `UAV-073` 查；多机用 `IN (...)`。非数字别名才先查 `uav_resource.alias` 获得机号。

`max_speed` 不是默认规划速度，`flight_remain` 不是已扣返航预留的预算；需要合格候选与预算时直接用候选准备参考，不重复折减健康度/电量。状态位置不是实时位置，任务航高不是表中的 altitude。载荷类型、探测距离和实际识别能力保持不同含义。

**geo_library**（地点）

| 字段 | 说明 |
|---|---|
| `region_name`、`alias` | 正式名称；别名以 `;` 分隔 |
| `longitude`、`latitude`、`altitude` | 地点入口坐标与海拔 |
| `region_shape`、`region_length_m`、`region_width_m`、`region_area_m2` | 登记的任务区域：形状、东西长、南北宽、面积；为空表示未登记区域 |
| `region_center_longitude`、`region_center_latitude` | 任务区域中心，与入口不同 |

“默认位置”是一个别名，查询写 `alias LIKE '%默认位置%'`。
仅方向+距离的指定单机请求优先沿用会话基准，否则需要该机有来源的当前位置；未指定执行机时用地点库“默认位置”。本库只提供起飞点记录。用户已指定该起飞点或会话已采用该基准时直接沿用；确需当前位置时查询可用实时来源，仍无法取得才询问。

## SQL 返回与异常

成功返回 `{"status":"OK","count":…,"truncated":…,"data":[…]}`，只读取 `data` 里实际返回的字段。

| 状态 | 处理 |
|---|---|
| `NOT_FOUND` | 核对全名、别名和已给地域后补查；仍无记录且缺任务必需事实时，询问具体地点或缺项 |
| `INVALID_ARGUMENT` | 对照本页常用字段或按需读取的扩展字段修正 SQL，最多重试一次 |
| `DATABASE_ERROR`、`IO_ERROR`、`ERROR` | 报告数据库或文件错误，停止依赖它的步骤 |
| `truncated=true` | 结果超过 100 行：收窄条件或分页，不把第一批当作全部 |

## 注意事项

- 经名称、地域和上下文筛选后仍有多个实际候选时，一次列出名称或区别供用户选择。用户已给完整坐标和区域时可直接交空间计算；只有依赖库中事实的缺项才需要补问。
- 不做表结构枚举（PRAGMA、sqlite_master），可用字段以本页及扩展字段参考为准；禁止写入。
- 状态与资源通过 `uav_id` 关联；查电量、任务状态这类动态信息时每次重新查询，不复用对话里的旧结果。
- 状态库的位置是起飞点记录，不是飞行中的实时位置；运行中的状态以中央任务 `get` 为准。
- 载荷类型和探测距离是两种约束，不能互相推导。
