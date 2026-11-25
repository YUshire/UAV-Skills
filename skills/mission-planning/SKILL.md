---
name: mission-planning
description: 总路由：完整飞行任务、只规划方案、可行性评估、多步/多机/条件/扫描/纯飞行混合任务、方案确认变更及任务事件。不管：单项查库、单项坐标换算、单项航迹预览、单项动作校验、单项感知或纯数值分配咨询。
---

# 任务总路由（互斥决策树）

## 第一步：按顺序命中唯一入口（严禁同时预读 `artifact-pipeline.md` 与 `task-graphs.md`）

从上到下匹配**第一条**命中的规则，只读对应参考页：

1. **复杂任务图（最高优先）**：指令包含以下**任一**特征 → **只读 [task-graphs.md](references/task-graphs.md)**（只规划不下发）：
   - 独立接口动作：`扫描(scan)`（如扫描战壕/道路）、`录像`、`拍照`、`持续观察`、**纯点到点飞行后的**`悬停/等待`、`切换模式(红外/高清)`、`发现后报告/跟踪`、自定义跟踪或持续时长；
   - 复杂逻辑：条件分支（找到/没找到/超时）、发现后动态位置（“找到后以它为中心/由最近机接手”）、跨机先后依赖（“A机完成后B机再…”）、或含**纯“飞往/去某地”途经点**的混合任务。
   - **防误判边界**：单区/多区搜索、覆盖或巡逻前的“飞抵/直飞/转场至该地（或相对偏移点）后开展作业”，以及“搜索/覆盖/巡逻完成后原地待命/悬停”，均由常规 `--pipeline` 原生支持（自动含转场段及 `on_search_complete:"HOLD"`），**不要**因此命中本条拆成 `transit`/`hover`，应直接走规则 2/3 生成可下发动作序列。
2. **同机多步混合**：无上述第 1 类特征，但同一架机内混合 `search`/`patrol` 或要求同机“先…再…”顺序 → 读 [combined-tasks.md](references/combined-tasks.md) + [artifact-pipeline.md](references/artifact-pipeline.md)。
3. **普通搜索 / 探索 / 巡逻 / 可行性评估**：单区或多区搜索（含无目标探索、两机分区共担）、边界巡逻，或“只规划/只回答可行性” → **只读 [artifact-pipeline.md](references/artifact-pipeline.md)**。

### 按需附加页（仅在对应环节触发时读，不预读）
- **准备最终交付回复**（完整方案、确认卡或交付落盘）→ [plan-delivery.md](references/plan-delivery.md)（常规规划交付无需再读 `flight-control/SKILL.md`）
- **任一计算/校验报错、不可行或返回条件备选** → [diagnostics.md](references/diagnostics.md)
- **中央下发 / 处理确认卡回复**（**仅当 `AGENTS.md` 开关为 `ONLINE_EXEC` 时可用；当前 `PLANNING_ONLY` 模式禁止读取与调用**）→ [online-submission.md](../flight-control/references/online-submission.md)
- **已提交任务的运行控制、待决策或故障补做**（仅 `ONLINE_EXEC` 模式）→ [runtime-control.md](../flight-control/references/runtime-control.md)

## 第二步：授权与约束边界
- **执行授权**：当前 `PLANNING_ONLY` 模式下，明确动作指令与只规划请求均连续完成规划、校验与交付落盘后直接按 `plan-delivery.md` 回复“方案已生成并检查，尚未下发”，不调用 `mission_client.py` 或启动感知；仅 `ONLINE_EXEC` 模式下再按授权下发。“只规划”“只回答可行性”“确认后执行”始终优先遵守。
- **改变原约束须待接受**：换机、减少点名机、共担改单机、改速/改航高、跨机顺序改并行、探索替代整区覆盖等备选方案，必须说明差异并等待用户接受，均不得静默采用。
- **机号与动作保真**：区分执行机与位置参照机；保留点名参与机、恰好出动数量（`required_drones`）与明确先后顺序；严禁把纯“飞往”篡改成区域 `search`。
- **确认卡有效范围**：确认仅对应最近一张待确认卡；修改后旧卡作废；故障补做沿原任务授权按 `runtime-control.md` 处理。
