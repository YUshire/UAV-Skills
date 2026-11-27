# 2026-11-27 默认航高对齐、路径模式解耦与任务图核算修复同步说明

基于远程 `origin/main`（`18b5010`，已合入 PR #9），本次同步 2026-11-26 至 2026-11-27 的可达性计算对齐、任务能力解耦、技能文档精简同步及回归测试更新。

## 改动清单（三段式）

1、
改动：在 `uav_runtime/budget_math.py`、`atomic/selection.py`、`atomic/allocation.py`、`planning_geometry.py`、`seven_geometry.py`、`seven_inputs.py`、`direct_path.py`、`area_route.py`、`task_graph.py`、`graph_planner.py` 及 `skills/flight-control/scripts/submit_queue_batch.py` 中对齐数据构造侧计算口径：未指定 `flight_alt` 时默认航高统一取 `min(50.0, max_altitude)`（原硬编码低航高导致覆盖条带过密）；按每架无人机自身 `fov` 与规划航高分别计算 `work_range_by_uav` / `work_time_by_uav`（消除候选池最小 `fov` 拖累）；圆形 `job_radius` 对齐为 `radius` 且圆形覆盖长度对齐 `search_circle_coverage_m()`；补充多任务联合探针 `_multi_job_probe`；将 `submit_queue_batch.py` 默认单段距离上限由 `20000 m` 调整为 `30000 m` 以覆盖数据库 `max_flight_radius`（最高 `25000 m`）。
原因：对比数据构造侧可达性校验脚本发现，旧默认 5 m 航高与候选池最小 `fov` 会显著虚增覆盖航程，导致苗栗通霄整区巡逻与宜兰苏澳→东澳湾连续覆盖等可达任务被误判超预算或拆趟。
影响：单机整区巡逻与单机多区连续覆盖的可达性判断与数据构造侧完全对齐，不再出现误判超预算或拆趟剩余电量倒挂。

2、
改动：在 `uav_runtime/area_route.py`、`seven_inputs.py`、`planning_geometry.py`、`seven_geometry.py`、`task_graph.py`、`graph_planner.py` 及 `skills/flight-control/scripts/assemble_plan.py` 中从第一性原理解耦任务语义与几何执行：拆分 `task_type`（`search` / `patrol`）与 `path_mode`（`coverage` / `boundary`），新增切入方位 `entry_side`（`south|north|east|west`）、绕行方向 `direction`（`ccw|cw`）与作业段速度 `work_speed`（转场保持巡航速度、作业段按慢速执行）。圆形覆盖巡逻（`patrol + coverage`）直接生成圆内航点输出 `patrol_move` 而非外接正方形；边界搜索（`search + boundary + laps=N`）支持多圈 `patrol_move` 并衔接 `found/not_found/timeout` 条件事件（含 `report` / `track`）。
原因：此前 `task_type` 与路径形状、圈数、事件绑定过紧，且缺少切入方位、顺逆时针及作业段独立降速字段，导致“南侧切入”被误报功能遗漏、圆形覆盖巡逻退化为外接正方形冲突、“绕飞 3 圈搜寻”触发死锁、贴边慢速搜索与跟踪分支无法表达。
影响：上述四类任务均可在统一的正交参数模型下原生规划与校验，无需枚举补丁。

3、
改动：在 `uav_runtime/graph_planner.py` 中补全已知坐标 `transit` 节点的水平航程、时长、耗电与预算核算，并去除对已知坐标转场误标的 `RUNTIME_TRAJECTORY` / `RUNTIME_GEOMETRY` 待定项；同步更新 `skills/mission-planning/references/plan-delivery.md` 与 `artifact-pipeline.md`、`diagnostics.md`、`task-graphs.md`、`data-query`、`trajectory-generation` 相关参考页，明确条件任务多分支在交付表格中增设「条件分支」列与下方动作 JSON 代码块严格一一对应（动作与预算完全相同的分支如未发现/超时可合并展示）。
原因：纯转场+原地待命任务虽已算出路径，但电量核算缺失导致回复显示“待实际路径确认后补充计算”；多分支条件任务上方表格仅汇总一行，与下方多个动作代码块不对应。
影响：已知坐标 `transit → hover` 直接输出完整航程与预计剩余电量，条件分支交付卡片与动作 JSON 严格对齐。

4、
改动：在 `tests/test_task_graph.py` 补充 3 项回归测试，覆盖已知坐标 `transit → hover` 预算核算、`search + boundary + laps=3 + direction="cw" + work_speed` 速度分层及 `patrol + coverage` 圆形覆盖与默认 50 m 航高。
原因：将本轮核心修复固化为离线可重复执行的自动化检查。
影响：全量单元测试增至 60 项并全部通过。

## 涉及文件

- `skills/data-query/SKILL.md`
- `skills/data-query/references/candidates.md`
- `skills/flight-control/scripts/assemble_plan.py`
- `skills/flight-control/scripts/submit_queue_batch.py`
- `skills/mission-planning/references/artifact-pipeline.md`
- `skills/mission-planning/references/diagnostics.md`
- `skills/mission-planning/references/plan-delivery.md`
- `skills/mission-planning/references/task-graphs.md`
- `skills/trajectory-generation/references/offline-preview.md`
- `skills/trajectory-generation/references/planning-routes.md`
- `uav_runtime/area_route.py`
- `uav_runtime/atomic/allocation.py`
- `uav_runtime/atomic/selection.py`
- `uav_runtime/budget_math.py`
- `uav_runtime/direct_path.py`
- `uav_runtime/graph_planner.py`
- `uav_runtime/planning_geometry.py`
- `uav_runtime/seven_geometry.py`
- `uav_runtime/seven_inputs.py`
- `uav_runtime/task_graph.py`
- `tests/test_task_graph.py`

## 验证记录

- 运行时单元测试：`uav_runtime/.venv/bin/python -m unittest discover -s tests -q` → `Ran 60 tests — OK`。
- 数据与入口自检：`uav_runtime/.venv/bin/python uav_runtime/self_check.py --data` → `status: OK`（`geo_library: 530`, `uav_status: 414`, `uav_resource: 414`）。
- 端到端模型复测（`xopglm52`，9 条定向回归集 `test_middle.jsonl`，含 2 条可达性计算对齐用例、6 条表述与能力对齐用例及 1 条纯经纬度替换用例）：9/9 全部通过，8 项历史问题逐一验证生效，纯经纬度用例不查地点库直接进入 `resolve_jobs.py` 几何计算且与地名版结果逐位一致。
