# 2026-11-26 默认返航与悬停规则、固定回复模板与能力超界拦截同步说明

基于远程 `origin/main`（`27e2f40`，已合入 PR #8 动作 JSON 组装与强制展示），本次同步 2026-11-25 至 2026-11-26 的技能规则、规划运行时及回归测试更新。

## 改动清单（三段式）

1、
改动：在 `uav_runtime/planning_geometry.py`、`seven_geometry.py`、`task_graph.py`、`graph_planner.py` 及 `skills/flight-control/scripts/assemble_plan.py` 中贯通 `return_to_home` 开关并同步更新 6 个技能文档：用户未指定结束动作时默认返航降落（按往返计算，`return_to_home` 默认 `true`）；仅当用户明确要求盘旋/悬停/待命/不返航（`return_to_home: false`），或任务图未显式传 `return_to_home` 且该机末节点本身为 `hover`/`wait` 时，按单程计算并在末尾以空参数 `{"cmd":"hover","params":{}}` 结束（不追加返航与 `land`）；同步修复 `_routes()` 漏传 `return_to_home` 与 `_timing()` 遍历 `hover` 步骤 `KeyError` 的问题，并在 `tests/test_task_graph.py` 补充回归测试。
原因：批测发现未指定结束动作时若默认不返航会与常规返航预期冲突，而仅靠 `return_to_home` 默认 `true` 又会在 `transit → hover` 末节点悬停且模型漏写顶层字段时错误追加返航与降落。
影响：未指定结束动作一律默认返航降落，显式盘旋/悬停及末节点为 `hover`/`wait` 的任务图稳定按单程原地悬停结束，动作队列完全通过 `submit_queue_batch` 校验，全量 57 项单元测试全部通过。

2、
改动：重写 `skills/mission-planning/references/plan-delivery.md`，写死模板 A（正常规划/能力内备选确认）、模板 B（超出能力边界拦截）与模板 C（在线执行状态）三套固定 Markdown 结构，并在 `AGENTS.md` 与 `mission-planning/SKILL.md` 中硬性约束严禁自由发挥排版。
原因：历史轨迹输出格式不稳定，部分回复带 Markdown 标题/表格，部分退化为无结构纯文本。
影响：所有规划、备选确认与能力拦截回复统一按固定 Markdown 模板输出，动作 JSON 块位置与结构保持一致。

3、
改动：在 `skills/mission-planning/SKILL.md` 新增「第零步：能力边界前置拦截」，并在 `artifact-pipeline.md` 与 `plan-delivery.md` 中同步明确：对超出系统能力边界的指令（逐圈环形推进、螺旋式由外向内/由内向外扫描、边巡边持续报进度、禁限飞按批准航线绕行、遇障升高后再恢复贴边、全程通信链路保障等）直接按模板 B 拦截告知无法下发并反问调整，严禁偷换任务意图兜底或输出不符原意的动作 JSON；仅在能力边界内因机型/预算受限时才允许提供备选方案待确认。
原因：此前兜底口径过宽，导致模型把不支持的螺旋/逐圈等超界需求擅自改成普通探索并宣称已完成规划，出现能力边界与回复脱节。
影响：清晰区分“能力内资源兜底”与“能力外直接拦截”，避免静默篡改用户任务意图。

4、
改动：在 `skills/mission-planning/SKILL.md` 与 `artifact-pipeline.md` 中补充弓字形覆盖意图归类规则，将表达全面往复扫描的近似表述（如弓字形、拉网式、筛网式、梳状等类似说法）交由模型按语义统一归类为弓字形（平行往复）区域覆盖走 `--pipeline`，同时与螺旋/逐圈等非平行超界路径清晰切割。
原因：不同模板对区域全面覆盖存在多种近义口语表达，需要统一路由同时避免罗列冗长词表。
影响：常规全面覆盖类表述可稳定路由至 `--pipeline` 规划并输出动作 JSON，非平行特殊路径仍准确触发前置拦截。

## 涉及文件

- `AGENTS.md`
- `skills/mission-planning/SKILL.md`
- `skills/mission-planning/references/artifact-pipeline.md`
- `skills/mission-planning/references/plan-delivery.md`
- `skills/mission-planning/references/task-graphs.md`
- `skills/flight-control/SKILL.md`
- `skills/flight-control/references/online-submission.md`
- `skills/flight-control/scripts/assemble_plan.py`
- `uav_runtime/planning_geometry.py`
- `uav_runtime/seven_geometry.py`
- `uav_runtime/task_graph.py`
- `uav_runtime/graph_planner.py`
- `tests/test_task_graph.py`

## 验证记录

- 运行时单元测试：`uav_runtime/.venv/bin/python -m unittest discover -s tests -q` → `Ran 57 tests — OK`（含新增的默认返航、显式 `return_to_home=False` 单程悬停及 `transit → hover` 末节点自动悬停回归断言）。
- 动作展示开关：当前 `assemble_plan.py` 中 `SHOW_ACTIONS = True`（延续 PR #8），所有已规划任务均附带标准动作 JSON 代码块；能力超界拦截走模板 B 不附带动作 JSON。
