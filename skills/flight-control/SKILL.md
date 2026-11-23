---
name: flight-control
description: 把已生成的任务航迹组装成每架无人机的原始 WGS84 动作序列，用 dry-run 做格式校验和坐标转换检查，并通过中央服务完成下发与运行控制。用于动作组装、校验、中央提交及运行处置。
---

# 动作组装、校验与执行路由

| 当前请求 | 读取与处理 |
|---|---|
| 从本次 build 产物生成完整动作 | 按下文组装，不另读数组字段或在线命令页 |
| 检查用户已有动作数组，或排查动作字段 | 读 [action-validation.md](references/action-validation.md)，仅做 dry-run |
| 确认、修改、取消待确认方案，或普通方案准备提交 | 读 [online-submission.md](references/online-submission.md)，执行确认与中央流程 |
| 已提交任务的查询、控制、事件或异常 | 读已有 [runtime-control.md](references/runtime-control.md) |

## 从规划产物组装

使用脚本直接读取本次 build 产物，完成组装、dry-run 并保存完整结果：

```bash
uav_runtime/.venv/bin/python uav_runtime/run.py skills/flight-control/scripts/assemble_plan.py \
  --input '{"plan":{"ref":"<build实际返回的artifact_file>","select":"/data"}}'
```

此脚本组装并校验，不下发（`submitted=false`）。命令响应与保存文件是两种结构，不猜字段位置：

| 读取来源 | 数据位置 |
|---|---|
| 本次命令响应 | `summary.participants[*]` 含电量、航程、航高及动作展示；`summary.show_actions` 是本次展示开关 |
| `artifact_file` 保存文件 | `data.participants[*].report` 是对应逐机报告；文件顶层没有 `summary` |
| 保存文件中的可注册动作 | `data.participants[*].raw_batch` 为原始 WGS84 完整数组 |
| 保存文件中的校验结果 | `data.participants[*].dry_run.batch` 为已转换的本地坐标数组，不能重新转换或注册 |

优先复用本次命令返回的报告。输出被截断且确实缺交付字段时，只按上述路径读取所缺参与机/字段；不整份打印产物或再次拼接所有动作造成截断。动作是否展示仍由本次 `summary.show_actions` 决定，不因文件里有动作而绕过关闭设置。

**动作展示开关**：修改 `scripts/assemble_plan.py` 顶部的 `SHOW_ACTIONS = True` / `False`；这是是否展示动作的唯一开关。模型沿用本次工具返回的布尔值 `summary.show_actions`，不得自行改变用户选定的开关：
- `true`：最终回复必须用 JSON 代码块展示本次交付方案实际返回的 `summary.participants[*].actions`；缺少动作结果时说明原因，不补造。默认长航点折叠为 `count/first/last`。
- `false`：最终回复不展示动作序列，保留任务安排、检查结果和必要文件链接；不另读 `raw_batch` 或手写动作绕过关闭设置。关闭展示不跳过组装、校验及完整动作产物保存。

原环境变量 `UAV_SHOW_ACTIONS` 仅保留展示格式选择：`full` 返回完整动作及 `local_batch`，其他值默认紧凑展示；旧值 `false` / `0` / `off` / `no` 不再关闭展示，关闭统一修改上述布尔开关。`summary.actions_format` 说明返回动作的展示格式。完整方案的其余回复内容按 [交付规范](../mission-planning/references/plan-delivery.md) 处理；单项校验仅回复本项结果。

组装失败时读 [组装诊断](../mission-planning/references/diagnostics.md#动作组装与校验)，据本次 `participants` 交付检查范围。

开启展示时，本次交付方案来自多次组装则汇集对应返回中的全部参与机，逐机展示实际返回的 `actions`；待接受的替代方案同样展示，并标明它改变的约束。未下发、原请求不可行或已经给出摘要表格，都不代替开启状态下要求的 JSON。只汇集本次交付采用的方案，不混入已放弃的版本。每机字段独立写明，不用“同上”或省略号代替；保留工具已有的航点折叠结构。紧凑动作仅供展示，实际注册仍使用产物中的 `raw_batch`。

普通任务离线交付时，另用 `write_file` 将本次 `task_key`、动作产物实际返回的 `artifact_file`、参与机、`targets` 和拟采用的 `policies` 保存到唯一的请求 JSON；无目标时 `targets=[]`，默认 `policies.land_on_return=true`；无目标任务结束后不返航时设 `on_search_complete="HOLD"`。跟踪须保留 `on_first_confirmed="APPROACH_AND_TRACK"` 和实际目标 ID 的 `tracking_target_ids`；线上 approach/tracking profile 引用须来自真实 status；尚未取得时列为待补，不伪造 ID 或 revision，在线绑定写法仅提交时读参考页。此文件仅保存意图，不是已授权或已提交的 spec，不能直接用于下发；接通后仍按中央流程获取真实引用并校验。

完整任务的速度沿用 build 的 `assignment.speed`（用户指定或逐机巡航速度），不是仅用户给速时才填写；组装器写入各动作的 speed/max_vel，不依赖执行端默认速度。按每机 steps 保留 search/patrol 类型、顺序、圈数和搜索时限；航点与搜索框不手改。动作字段的具体映射仅调试时读数组校验页。

## 航高与结束策略边界

`fuel_explore` 的 `box_min[2]`/`box_max[2]` 是搜索空间上下界，当前组装接口没有独立的定高参数；例如 80 米方案的 `[0.3,80.5]` 不证明探索全程保持 80 米。用户明确要求定高时，保留已算的几何方案并说明搜索阶段定高尚未验证，不把格式校验成功说成定高要求已满足，也不靠手改搜索框实现定高。

用户要求无目标探索/巡逻完成后原地悬停时，仍保留 `build` 给出的返航转场和 `land`，作为中央登记需要的备用收尾段；另在方案中明确 `policies.on_search_complete="HOLD"`。中央把作业段和返航段分开，由策略或后续返航指令决定是否执行收尾，不是把整个 batch 直接连续发布。离线只验证格式与转换，不能称已经验证悬停执行或无限等待续航；不能删除返航段来“实现HOLD”。

转换范围不是物理航程：组装读取转换器当前默认值，采用本次 `summary.max_distance_m`（失败时 `max_distance_m`），不记忆公里数；候选中心预筛不代表全部航点已通过。单项显式范围参数见数组校验页。

中央接管的机只用中央提交和运行控制，不直接发布动作队列或 ROS 话题。完整任务、只规划及备选接受边界按 mission-planning；单项数组校验不强加完整搜索/巡逻要求。
