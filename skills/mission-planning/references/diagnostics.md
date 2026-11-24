# 失败诊断 SOP 速查表

> 仅在任一阶段报错、不可行或出现条件备选时按表定位，**禁止翻阅源码或拆读大产物**。`--pipeline` 失败时已自带 `failed_step`、`last_ok_summary` 与诊断字段；任务图失败已自带 `summary.scenarios[*].violations`/`pending`。

## 错误码 → 唯一补查动作 → 必须交付内容

| 触发错误 / 信号 | 唯一补查动作（最多 1 次） | 必须交付内容（日常中文） |
|---|---|---|
| **`CANDIDATE_EXCLUDED`** / **`COORDINATE_RANGE_EXCEEDED`** / 指定机超半径或转换上限（含 `retained_count > 0`） | ① 先看返回中的 `available_candidates` / `retained_uav_ids`；② 若为空或需周边替代机，**立刻去掉 `uav_ids`、带作业中心调 1 次 `prepare_candidates.py`** 查周边合格替代机；③ 其余独立子任务正常继续规划 | 1. 准确区分绑定限制（**转换保护上限** `max_distance_m` 不是物理航程；`FLIGHT_RADIUS` 超半径提高速度无解；`max_recorded_altitude` 升限不足）；2. 给出有据可查的替代机备选并标明**待用户接受**；3. 禁止凭单机或被排除列表断言“全机队无解” |
| **`NO_FEASIBLE_ALLOCATION`**（含 `SHARED_JOB_TIME_BUDGET`）或成本返回 `alternatives` / `altitude_alternatives` | ① **多组独立任务仅部分组失败时，必须剥离失败组、对其余独立组跑完完整 `--pipeline` 并交付**（绝不因一组失败放弃其余可独立完成的组）；② 对失败组按 `alternatives.items` 建议速度（核对 `max_speed`）或将默认航高提到 `2/3/5 m` **最多试算 1 次条件备选**（绝不删原 `required_drones` 或手拆共担作业伪装完成） | 1. 失败组的精确瓶颈（含 `diagnosis` 到达/等待/超限秒数）；2. 有证据的提速/提航高/单机调整建议（标明**待用户接受**，提航高须提示分辨率影响）；3. 其余独立子任务的完整可执行方案 |
| **`TIME_RESERVATION`** / **`DEADLINE_RESERVATION`**（任务图 `pending`） | 无需重跑（或按需试算 1 次缩短 `search_timeout`） | **必须如实告知**按默认搜索时限/起降/返航预留的时间超出续航预算（报出具体秒数或分钟），说明不代表物理无解，并提示可缩短搜索时限 |
| **`assemble_plan` 部分机失败**（如单机航点超 `max_distance_m`，`artifact_created=false`） | ① 直接复用 `pipeline` 返回的 `last_ok_summary` 逐机区分状态；② **严禁私改上游 `build` 产物删掉失败机重跑子集 `assemble_plan`，且禁止生成完整交付请求 JSON**；③ 本项属动作组装转换超限（非预筛 `CANDIDATE_EXCLUDED`），最多按第 1 行查 1 次周边替代机作待接受建议后立刻收敛 | 逐机区分已计算航迹、检查通过与检查失败（报出动态 `max_distance_m`）；说明完整方案尚未通过、不可直接执行，后续条件保留未解决项 |
| **纯飞行/预览总航程已超 `range_budget`** | **硬截断，不重试、不套跑 `--pipeline`** | 直接报出各段预览航程合计与可用航程预算差距，禁止反事实假设（不返航/提速/满电） |
| **`INVALID_ARGUMENT`** / **`INVALID_REFERENCE`** | 仅在不改用户原值且修正明确时修正参数重试 **1 次**；仍失败则停止 | 用自然语言说明参数冲突或受阻原因（用户原值非法如负速/超升限时直接询问，不自改） |
| **`SOLVER_TIMEOUT`** / **`SOLVER_FAILED`** / **`SOLVER_UNAVAILABLE`** / **`ENVIRONMENT_ERROR`** | 立即停止依赖链，不重试同一确定性缺失入口 | 如实说明求解或外部服务不可用（不能表述为物理不可行），交付已可独立完成部分 |

## 关键区分红线
- 缺信息 ≠ 不可达；默认参数失败 ≠ 物理无解；局部候选池失败 ≠ 全机队物理无解；坐标转换阈值 `max_distance_m` ≠ 物理航程/飞行半径。
- 仅在有证据的 1 次速度/航高/换机补查后立即收敛交付，绝不反复试探。
