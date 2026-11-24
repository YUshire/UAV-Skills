---
name: spatial-compute
description: 【空间换算】负责：相对方位距离偏移（calc_offset）、单项区域整理（resolve_jobs）与 FUEL 搜索框计算（build_fuel_box）。不负责：查库、航迹与完整规划（普通完整任务的无偏移作业已在 artifact-pipeline 内给出 Schema，无需另读本技能）。
---

# 空间计算（唯一空间换算权威源）

所有坐标为 WGS84 经纬度，距离、尺寸、高度单位为米；返回值直接使用，**不手算、不取整**。

## 1. 相对位置偏移（`calc_offset.py`）
```bash
uav_runtime/.venv/bin/python uav_runtime/run.py skills/spatial-compute/scripts/calc_offset.py \
  --lon <参考点经度> --lat <参考点纬度> --bearing <方位角> --distance <距离m>
```
- `--bearing`（顺时针，正北 0°）：北 `0`、东北 `45`、东 `90`、东南 `135`、南 `180`、西南 `225`、西 `270`、西北 `315`。`--distance > 0`，`--alt` 不传。
- **相对基准规则**：
  1. 地点基准：有登记区域时取 `region_center_longitude`/`region_center_latitude`，否则取地点入口 `longitude`/`latitude`；
  2. 仅方向+距离且指定单架执行机：需该机有来源的当前位置（状态库起飞点不能冒充实时位置，除非用户/会话已明确沿用起飞点，否则询问）；未指定执行机时查地点库“默认位置”；
  3. 左/右/前/后须有朝向证据，否则询问（禁止将“左前方”固定当西北）。
- **注意**：偏移距离 ≠ 区域半径或长宽，严禁混用。

## 2. 区域整理（`resolve_jobs.py`）
通过 `run.py skills/spatial-compute/scripts/resolve_jobs.py --input '<JSON>'` 调用，输入 `{"jobs":[...], "places":[...]}`：
- **地名作业**：`{"job_id":"J1","place":"教学楼"}`（用户给定范围时加 `shape` 与尺寸覆盖登记区），同级**必填** `places`（`query_location.py` 返回的 `data` 原数组）。
- **坐标作业**：`{"job_id":"J1","name":"区域1","center_lon":...,"center_lat":...,"shape":"circle","radius":10}` 或 `shape:"rectangle","length":东西长,"width":南北宽`。全部为坐标作业时可省略 `places`。
- 可选字段：`uav_id`、`task_type`（`search`/`patrol`）、`laps`、`search_timeout`。仅登记点位且用户未给范围时返回 `MISSING_PARAMETER`，须询问形状与尺寸。

## 3. FUEL 搜索框（`build_fuel_box.py`，仅单项请求用）
```bash
uav_runtime/.venv/bin/python uav_runtime/run.py skills/spatial-compute/scripts/build_fuel_box.py \
  --center-lon <经度> --center-lat <纬度> --shape rectangle --length <东西长> --width <南北宽> --flight-alt <航高>
```
圆形用 `--shape circle --radius <半径>`（返回外接正方形）；`--flight-alt > 0.3`。

## 异常
`MISSING_PARAMETER` 先从上下文/库补齐，仍缺则询问；`INVALID_ARGUMENT` 检查范围与正数约束重试 1 次；`ERROR` 停止并报告。
