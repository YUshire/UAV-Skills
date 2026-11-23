# 渐进式披露改动与审查记录（2026-11-20）
本文是**第一阶段验收时的历史记录**：下文迁移位置、40条链接和体积数字均指该阶段，不代表后续完整技能结构。第二阶段对六技能内部的拆分、当前迁移位置与验证口径见 [六技能按需阅读记录](skills_lazy_loading_review_20261120.md)。

## 判断、原因与范围

原入口 `always: true` 会让 nanobot 把完整规划正文注入每次系统上下文；单项查询无须这些规则。采用 AGENTS 路由、短入口、按场景参考页，可以解除该常驻依赖。此次仅修改主工作区文档，不改脚本、调用参数、默认配置或数据库；不推送 GitHub。

按 `docs/审查框架` 核对意图与约束保真、能力边界、授权、产物和效率证据。当前用户已明确授权修改，框架中的只审查边界不替代本次授权。原文临时快照位于 `/tmp/uav-disclosure-before-20261120/`；协作由 Codex 实现、Claude 审查，意见与验收记录在 `.collab`。

## 规则迁移去向

| 原内容 | 现位置及触发条件 |
|---|---|
| 单项六技能分类 | `AGENTS.md`：直接读取对应既有技能；混合查询的完整飞行任务仍进主入口 |
| 意图、执行授权、确认边界、真实状态 | `mission-planning/SKILL.md`：完整任务、确认、运行指令或事件才读 |
| 日常表达、区域/相对基准、候选角色/数量、默认值与普通计算链 | `references/artifact-pipeline.md`：普通完整规划；任务图仅在缺公共候选或区域规则时查对应小节 |
| 同机逐步参数与混合动作 | 原 `references/combined-tasks.md`，保持内容不变 |
| 跨机依赖、条件/动态位置、接口与时长边界 | 原 `references/task-graphs.md`，补回迁移的独有解释；仍只规划 |
| 确认回复、中央前置检查与下发顺序 | 原 `flight-control/SKILL.md`；仅确认处理或准备下发时使用该流程 |
| 运行查询/控制/决策与异常补做 | 原 `flight-control/references/runtime-control.md`；诊断询问本身不授权重新起飞 |
| 回复检查、确认卡、逐机结果及预算适用范围 | 新 `references/plan-delivery.md`：完整方案或结果交付前读 |
| 速度/航高/航程、共担等待、组装和引用错误 | 新 `references/diagnostics.md`：失败或返回条件备选时仅查对应阶段 |
| 动作展示布尔、原始 WGS84/本地坐标及备用收尾段 | 仍由原 `flight-control/SKILL.md` 定义；其他页面引用，不再复制一套开关说明 |

参考路径以技能目录为根定位。修正各单项技能回读旧主入口及“第五节”等失效引用；成本技能仍保留接口返回字段契约，失败回复解释集中至诊断页。

## 路由审阅样例（静态核对，不冒称模型复测）

| 输入情形 | 应读入口与边界 |
|---|---|
| 查 UAV-001 电量；教学楼在哪；找带红外相机的空闲机 | data-query；仅查候选/载荷不启动任务，不读主规划技能 |
| 教学楼东北 20 米的坐标 | spatial-compute，必要时 data-query；不启动完整规划 |
| A 到 B 航线预览 | trajectory-generation；不下发 |
| 检查已有动作数组 | flight-control 的单项校验；不强加完整任务 |
| 单独开启识别 / 数值分配咨询 | target-detection / mission-orchestration |
| 查好电量后派合格机巡逻 / 只规划搜索 | 主入口 → artifact-pipeline；限定只规划时不启动感知或下发 |
| 同机先巡逻再搜索 | 主入口 → combined-tasks 与普通计算链；保留顺序和每步参数 |
| A 机完成后 B 机执行 / 自定义跟踪时长 | 主入口 → task-graphs；保留依赖与时长，不把已知前段直接下发 |
| 找到目标后以它为中心搜索 | 主入口 → task-graphs；保留运行时引用，不追问未来坐标 |
| 确认、修改或取消最近方案 | 主入口 → flight-control 的确认回复表；旧卡失效规则保留 |
| 暂停、目标发现事件、为什么停了 | 主入口 → runtime-control；只问原因时仅查询 |
| 成本/分配/组装失败 | 对应 diagnostics 小节；保留点名机、数量、等待预算和逐机失败证据 |
| 开关 true/false 下交付动作 | 原 flight-control 规则；true 展示实际返回动作，false 不展示且不跳过组装保存 |

## 本地验证证据与复现

只读检查命令：`PYTHONDONTWRITEBYTECODE=1 python3 /tmp/uav_disclosure_checks.py`。该临时检查不进入交付脚本，验证结果如下：

- 使用实际 nanobot `SkillsLoader`：改前常驻列表含 mission-planning，改后不含；技能摘要仍可发现它。
- 使用实际 `ContextBuilder.build_system_prompt`（只读 memory 替身）：新系统提示含 AGENTS 路由及技能摘要，不含 mission-planning 正文。
- 改动文档的 40 条相对链接及锚点均存在；原流水线首个可运行 JSON 示例逐字未改。
- 55 个技能/runtime Python 文件相较修改前哈希一致；主工作区仍是 nanobot workspace 符号链接的目标。
- 主入口由 40013 字节、268 行变为 4531 字节、40 行（约减少 88.7%）；这是文档体积，不等于 token 或延迟实测。

验收命令由 `.collab/collab.py verify` 记录最终结果：

```bash
env PYTHONPATH=. ./uav_runtime/.venv/bin/python -m unittest discover -s tests
env PYTHONPATH=.:chat_with_bot:/data01/whyu11/nanobot python3 -m unittest chat_with_bot.test_chat_output chat_with_bot.test_reply_json
```

另执行 skill-creator 的 `quick_validate.py` 检查变更的七个技能元数据；这些检查证明加载、引用及现有脚本兼容性，不证明模型所有路由样例已实际运行。

## 限制与后续建议

未新增付费模型复测，未下发任务，未测量首字延迟或本轮 token 消耗。新构建的系统上下文适用新路由；旧会话已读取的技能文本仍可能保留在历史里，不能声称会立即消失。后续若复测，应区分新会话初始注入量、按需读取量与实际任务完成质量，保留原始轨迹作比较。

既有 F-008 效率问题独立记为 documented partial：补017未再读源码/复制组装，但工具总数没有下降；行李箱复测缺最终回复，349.777 秒 API 长尾尚未归因。详见 `docs/efficiency_review_20261119.md` 的“验证与边界”和对应 retest 归档；本次文档改动不把它升级为端到端效率已修复。
