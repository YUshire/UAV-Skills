---
name: flight-control
description: 【单项校验与在线飞控】负责：校验用户已有动作数组（dry-run）、在线中央提交（仅 ONLINE_EXEC 模式）及运行中控制/故障补做。不负责：常规规划阶段的动作组装与方案交付（当前 PLANNING_ONLY 模式禁止读取本技能做中央提交）。
---

# 飞控校验与在线执行路由

| 当前请求 | 读取与处理 |
|---|---|
| 校验用户已有动作数组 / 排查动作字段 | 读 [action-validation.md](references/action-validation.md)（仅 `--dry-run`） |
| 确认/修改/取消待确认方案，或执行中央提交（仅 `ONLINE_EXEC` 模式） | 读 [online-submission.md](references/online-submission.md) |
| 已提交任务的查询、控制、待决策、事件或故障补做（仅 `ONLINE_EXEC` 模式） | 读 [runtime-control.md](references/runtime-control.md) |
| 手动从单步 `build` 产物组装动作（非 `--pipeline`） | 按下方命令调用 `assemble_plan.py` |

## 单步动作组装（`assemble_plan.py`）
```bash
uav_runtime/.venv/bin/python uav_runtime/run.py skills/flight-control/scripts/assemble_plan.py \
  --input '{"plan":{"ref":"<build实际返回的artifact_file>","select":"/data"}}'
```
- 仅组装与离线格式/坐标转换校验（`submitted=false`）。返回的 `summary` 已包含 `task_key`、`jobs`、`participants`（含 `speed`、`origin`、`takeoff_to_jobs_m`、预计航程与剩余电量）、`show_actions`、`budget_scope`、`max_distance_m`。
- 动作展示开关由脚本顶部 `SHOW_ACTIONS` 控制（当前默认 `True`）；**只要产出可下发动作序列且 `summary.show_actions` 为 `true`，最终回复必须用合法 JSON 代码块逐机展示动作序列**（细则见 `mission-planning/references/plan-delivery.md`）。

## 航高、结束策略与安全边界
- **定高边界**：`fuel_explore` 的 `box_min[2]`/`box_max[2]` 是搜索空间上下界而非全程定高参数；用户明确要求定高时，保留几何方案并说明搜索阶段定高尚未验证，不手改搜索框。
- **悬停结束（HOLD）**：无目标探索/巡逻要求完成后原地悬停时，保留 `build` 的备用返航与 `land` 段，另在 `policies` 设 `"on_search_complete":"HOLD"`（严禁删返航段实现 HOLD）。
- **转换范围**：动态采用本次返回的 `max_distance_m`，不写死公里数，不与物理航程混淆。
- **中央控制边界**：中央接管的机严禁去掉 `--dry-run` 直接发布动作队列或直发 ROS 话题；严禁将 `dry_run.batch` 已转换本地坐标二次转换或注册。
