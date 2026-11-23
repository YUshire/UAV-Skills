# 校验用户已有的动作数组

仅在用户提供动作数组并要求格式校验，或明确排查动作字段时读；已有 build 产物直接用主入口的 assemble_plan，不手写动作。

每条动作：`{"cmd":"<动作>","task_id":"<task_id>","step_id":<序号>,"params":{…}}`。同一序列的 `task_id` 相同，`step_id` 从 1 起按顺序加 1。

| 动作 | 必须写的 params | 速度字段（完整任务沿用规划值） |
|---|---|---|
| `arm` | `{}` | — |
| `takeoff` | `altitude`（该机 `flight_alt`） | `speed` |
| `follow_path` | `waypoints`、`"skip_unreachable": false` | `speed` |
| `fuel_explore` | `box_min`、`box_max`、`timeout_sec` | `max_vel` |
| `patrol_move` | `waypoints`、`"skip_unreachable": false` | `speed` |
| `land` | `{}` | — |

- 完整任务的速度由规划结果写入，不限于用户显式给速；单项数组校验缺速度时由校验脚本补默认值（当前为 1.2/0.6 m/s），不把该默认值当作完整任务规划速度。
- 航点格式 `{"longitude":…,"latitude":…,"altitude":…}`，可带工具返回的 `pass_through`；搜索框格式 `[经度,纬度,高度]`。

## dry-run 命令与结果

```bash
uav_runtime/.venv/bin/python uav_runtime/run.py skills/flight-control/scripts/submit_queue_batch.py \
  --json '<动作序列 JSON 数组>' --origin-lat <origin.latitude> --origin-lon <origin.longitude> --dry-run
```

`--origin-lat/--origin-lon` 取该机航迹结果里的 `origin`（起飞点），不能用任务中心或某个航点代替。

坐标转换范围是工具参数，不是飞机航程，也不是固定的业务半径。完整方案组装的范围按 [主入口](../SKILL.md) 返回值；单项校验显式传入 `--max-distance-m` 时以该次返回的 `conversion.max_distance_m` 为准，未传则用脚本当前默认值。回复按本次返回值换算单位，不沿用旧运行或文档示例中的数值；配置已变时旧预筛结论需重新核对。候选中心预筛不代替全部航点校验；超限时保留几何结果、说明当前配置限制，不擅改原点或参数来使校验通过。

| 结果 | 处理 |
|---|---|
| `status=OK`，`submitted=false` | 校验通过 |
| `INVALID_ARGUMENT` | 按 `message` 修正序列后重试一次，仍失败就报告 |
| `ERROR` | 报告错误并停止 |

返回中的 `batch` 是已转换为该机 MAVROS 本地坐标、补齐默认参数的 ROS 飞控动作序列；它不能再交给 register-plan 或任何转换命令（不能二次转换）。任务规划使用[主入口](../SKILL.md) 的组装脚本保存并交付完整结果；单项“校验动作序列”请求用这条命令，已有数组文件可用 `--file <路径>` 代替 `--json`。

通用 dry-run 不执行中央任务的作业/收尾校验：纯转场数组、删掉返航段的数组可能通过格式校验，但不能据此称当前中央任务支持它。完整任务还须满足[在线提交](online-submission.md) 的中央计划规则；单项动作校验无需强加搜索或巡逻。


## 组装映射（仅排查步骤转换时）

对任务航迹 `planning_routes.py` 的 `build` 结果中的每个 `assignments[i]`（完整数据在返回文件的 `data` 中）：

1. `task_id` = `<task_key>-<uav_id>`，例如 `T1021153012a3f2-UAV-003`。
2. 依次写：`arm` → `takeoff`（`altitude` = `flight_alt`）→ 按 `steps` 顺序转换 → `land`。同一架机的 `steps` 可混合 `search` 和 `patrol`，各步按自己的类型、`laps` 和 `timeout_sec` 组装，不套用整任务的单一类型：
   - `transit` → `follow_path`，`waypoints` 原样复制；
   - `search` → `fuel_explore`，复制 `box_min`、`box_max`、`timeout_sec`；
   - `patrol` → `patrol_move`，`waypoints` 原样复制，重复 `laps` 次（每圈一条动作）。
3. 航点和搜索框逐字复制，不取整、不删点、不调整顺序。
