# 单项离线航迹预览

仅用户需要两点航线、距离或单项区域路径时读；不调用成本分配链，不据此下发。命令前缀遵循 AGENTS.md。

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

本地离线入口没有 `query-map`。`semantic_map.py` 是外部语义地图入口，需要部署 `uav_runtime.map_provider` 及其地图资源；缺失时返回 `ENVIRONMENT_ERROR` 和具体组件名。只有已部署外部语义地图时才使用该入口。

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
