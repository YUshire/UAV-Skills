# 在线感知会话

仅用户要开启/停止识别、查询进度，或任务已获授权且准备在线下发时读；只规划及离线校验不启动感知。

下列在线命令需要已部署的外部运行项目、ROS 环境和中央服务；先按 AGENTS.md 检查环境。离线目标校验见 [主入口](../SKILL.md)，不需要读本页。

```bash
source "$ROS_SETUP" && source "$CATKIN_SETUP" && \
/usr/bin/python3 "$MIGRATION_ROOT/scripts/uav_perception.py" <命令> --uav-id <uav_id> --query-json '<JSON>'
```

| 命令 | 用途 | `--query-json` | 成功返回 |
|---|---|---|---|
| `status` | 查该机感知是否在运行 | 不需要 | 状态 JSON；`NOT_RUNNING` 表示未运行 |
| `start` | 启动感知并设置目标清单 | 目标清单对象 | `status=STARTED`、`ready=true`、`session_id`、`query_id` |
| `query` | 感知已在运行时，换成新的目标清单 | 目标清单对象 | `state=ACCEPTED`、`query_id` |
| `progress` | 查已找到和未找到的目标 | `{"search_id":"<search_id>"}` | 已找到与剩余的 `target_id` |
| `stop` | 停止该机感知 | 不需要 | `state=STOPPED` |

感知始终以默认的 `detect-and-continue`（只检测，不接管飞行）模式运行，不加 `--mode`。

## 下发准备与单项请求

1. 对每架参与机（可并行）：先 `status`；返回 `NOT_RUNNING` 用 `start`，已在运行用 `query` 换成本任务清单（会话不变，`query_id` 换新）。
2. 全部成功后，用 `mission_client.py status` 读 `fleet.<uav_id>.perception`，作为该机 `register-plan` 的 `perception`；这里的值才是中央认可的当前会话。
3. 任一机失败：停止流程，不下发飞行；本任务新启动的感知全部 `stop`。

## 单项请求

- 开启识别：机号和目标优先从本轮指令、当前任务及会话已明确的对象取得；仍有多架候选且无法判断指向，或完全没有识别对象时，才询问缺项。不生成飞行动作。
- “还剩哪些目标”：`progress` 查一次后回答，不循环查询。
- 停止识别：`stop`。

## 失败处理

| 情况 | 处理 |
|---|---|
| 目标清单格式报错 | 按报错修正一次 |
| “感知未产生有效观测”、启动超时 | 报告该机感知启动失败，停止依赖它的下发 |
| “本机感知实例已经运行或正在启动” | 改用 `query` |
| “先停止本机目标行为，再修改其感知查询” | 该机有旧目标行为在运行：报告，不自行停止正在执行的任务 |
| “感知查询未确认”或被拒绝 | 报告；不能把旧会话当作新清单已生效 |

- 跟踪由中央任务策略执行（见 [中央下发](../../flight-control/references/online-submission.md) 的 authorize）。感知只用默认的检测模式启动；不要用 `track-target`、`detect-and-return` 模式，也不要用 `start_target_tracking.py` 启动旧的目标行为节点。
