# 六技能按需阅读：改动与审查记录（2026-11-20）

## 判断与边界

前三份技能混合了单项查询、完整规划和在线执行说明，同一次 read_file 会把当前请求用不到的模式读入上下文。本轮仅拆文档和纠正已核实的契约描述；由 Codex 实现、Claude 审查，不修改脚本、参数、默认配置、数据库或原始轨迹，不推送 GitHub、不调用付费模型。

审查依据为 docs/审查框架：保留原意图与约束、区分计算和执行状态、保持参数权威来源，不能用减少校验或动作展示来压低长度。改前文档及受保护文件哈希快照在 `/tmp/uav-skills-stage2-before-6w34p1st/`。

## 实现与迁移矩阵

| 技能 | 入口保留 | 按需读取 |
|---|---|---|
| flight-control | build 组装命令、stdout/产物字段位置、动作展示布尔、原始/本地坐标、速度继承、定高/HOLD/备用收尾边界 | 数组字段与校验→action-validation.md；确认回复、中央下发、错误表→online-submission.md；运行处置复用原 runtime-control.md |
| trajectory-generation | 两点/区域预览与完整 costs/build 的模式路由、单位与地图能力边界 | 单项参数→offline-preview.md；完整成功路径契约→planning-routes.md；详细筛查证据与失败解释→已有 mission-planning/references/diagnostics.md |
| data-query | 只读 SQL、正式名/完整别名、常用字段归属、UAV-073 归一化、区域中心/入口与位置来源、异常停止 | 扩展字段→table-schema.md；执行候选与预算→candidates.md。记录查询不强加执行候选筛选 |
| target-detection | 目标清单、扫描对象区别、离线校验、实体计数和能力边界、发现通知 | 在线会话命令、下发前准备与会话错误→online-perception.md |
| mission-orchestration | 保留单文件及全部约束、矩阵、分组和求解器选项 | 将引用成本的常用示例前移；不声称重排本身减少读取量 |
| spatial-compute | 原文件保持不变 | 已有三入口导航及相对基准足够短，本轮不新增引用层 |

所有新增参考均明确触发条件。普通离线任务不读取在线提交/感知细则；完整 costs/build 不额外调用单项预览；单项预览不进入分配链。旧 mission-planning、普通流水线首个 JSON 示例、combined-tasks、task-graphs、runtime-control 和 AGENTS 均保持原文。

## 两处纠正及产物契约

1. 动作速度列改为沿用已有规划速度。assemble_plan.py 从 assignment.speed（或 plan.speed）取值并写入 speed/max_vel，并非仅用户显式给速才写。单项数组校验的缺省速度与完整规划速度仍分开。
2. candidates 的可选 planning_speed 参与数值分配：存在时按 work_range/planning_speed 求逐机作业时间，缺省才用 costs.work_time；引用完整候选时不得裁掉它。不改求解代码。
3. 命令响应的 summary.participants 与磁盘产物的 data.participants[*].report 明确区分；磁盘文件没有顶层 summary。优先复用当前响应；被截断时按已知路径只读缺少的机或字段，避免反复整份打印。动作开关仍取本次响应，不用文件里有动作推断开关。

保留 SHOW_ACTIONS true/false 的展示行为、折叠航点、自足 JSON、点名机与数量、共担/等待、成本与分配同源、动态转换阈值、跟踪目标与策略保存、确认与在线下发顺序。未修改最新轨迹中两套方案的交付规则，也未把最后回复的动作数当成应该削减的目标。

## 静态阅读体积（UTF-8 字节，不是实测 token）

| 路径/情形 | 改前 | 改后 |
|---|---:|---:|
| flight-control 入口 | 16353 | 6611 |
| trajectory-generation 入口 | 12410 | 1475 |
| data-query 入口 | 11005 | 5405 |
| target-detection 入口 | 7609 | 5045 |
| mission-orchestration 入口 | 6011 | 6272 |
| spatial-compute 入口 | 4294 | 4294 |
| 两点预览（入口＋命中参考） | 12410 | 5243 |
| 常规完整规划六技能层（含候选、costs/build参考） | 57682 | 41132 |

前三入口合计从39768变为13491字节，约少66.1%；常规完整规划的六技能层连同所需参考约少28.7%。后者不包含未改的 AGENTS、主规划及其参考，也不包括失败或在线场景另需的页面；二者均非实际调用、token、时延或模型正确率的测量。

## 验证范围与审查样例

临时检查：`PYTHONDONTWRITEBYTECODE=1 python3 /tmp/uav_skills_stage2_checks.py`。

- 13份变更技能文档，所有技能文档中的63条相对链接及锚点可解析。
- 原数据字段表中的47个字段名仍有说明；常用字段表强调资源/状态归属，扩展字段保留原表归属。
- 55个技能/runtime Python 文件、3个数据库哈希不变；AGENTS、spatial、主规划、流水线和运行控制等受保护文档逐字不变。
- 实际读取已保存的六机动作产物，验证 data.participants[*].report 与原始/本地动作路径；关联 build 的 data.assignments 含文档声明的预算和估计字段。
- 实际 nanobot SkillsLoader 可发现七技能，mission-planning 仍不常驻注入。仅改文档，不新增加载器逻辑。

静态路由核对：查电量→data 主入口；查抗风或探测距离→扩展字段；为执行准备候选→候选参考；两点距离→trajectory 预览；完整搜索→costs/build与组装；已有数组→数组校验；确认/提交→在线提交；暂停/故障→原运行控制；只规划目标清单→target离线校验；开启识别→在线感知。检查归一化73号机、动态阈值、严格定高、HOLD、显式跟踪、共担数量与待接受备选，防止拆分后遗漏必要规则。

最终回归由 collab verify 记录：七技能 quick_validate，46项 runtime 回归与6项本地聊天入口/JSON回归；聊天回归使用 fake bot，不访问模型。复现命令为：

```bash
env PYTHONPATH=. ./uav_runtime/.venv/bin/python -m unittest discover -s tests
env PYTHONPATH=.:chat_with_bot:/data01/whyu11/nanobot python3 -m unittest chat_with_bot.test_chat_output chat_with_bot.test_reply_json
```

这些验证覆盖引用、加载、既有脚本和已保存产物结构，不能证明模型一定按路由阅读或不再重复推理。最终审查及验收状态以 .collab 记录为准。

## 未覆盖

未执行新的付费模型复测，未验证端到端字符、token和延迟改善，也未降低推理预算、工具输出限制或关闭动作展示。此前F-008仍只算 documented partial；本次文档变化不把该效率问题升级为已经彻底解决。
