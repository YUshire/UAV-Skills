# 同步2026-11-23协作改动（2026-11-24发布，待用户合并）

远端 main 已通过 PR #5 合入上次更新，本次从 `f9ecfdb` 新建 `review/20261123-collaborative-updates`；只推送审查分支，不合并 main。

1、
改动：分配入口新增 extra_constraints，在成本约束上叠加用户指定归属、候选集合和出动数量。
原因：原流水线引用完整成本约束后，模型仍需手工拆读或拼接额外约束。
影响：允许集合取交集，单机要求取并集，冲突的固定归属和数量明确报错，保留原约束。

2、
改动：组装与任务图摘要补充作业、速度、起飞点距离、分支预算、告警及动作展示状态。
原因：回复所需字段不足导致模型读取源码和大产物反复排查。
影响：可直接引用工具摘要交付，完整动作产物与执行状态边界保持保留。

3、
改动：补充纯飞行混合任务、可行性评估、目标类别与离线交付文件的技能约束。
原因：批测出现飞往被改成搜索、凭坐标目测可行性、反复读源码和漏保存交付请求的问题。
影响：保留任务语义并优先使用现有契约，目标格式通过仍不等于实际识别能力通过。

4、
改动：取消规划前强制运行 self_check.py --environment，并同步清理七处关联说明。
原因：用户要求暂时跳过重复环境检测，直接开始任务查询和规划。
影响：遇到实际环境错误仍如实报告，中央 status、authorize、preview、submit 检查原样保留。

5、
改动：同步当前运行版本的 SHOW_ACTIONS=False，保留布尔开关的开关能力。
原因：上次仅发布文档，当前本地动作展示配置尚未同步到仓库。
影响：默认隐藏动作JSON，但继续组装、校验和保存完整动作，需展示时仍可切回True。

6、
改动：修复流水线测试对动作必展示的假设，补充约束合并与摘要回归，并附评分规范和49题对比报告。
原因：昨天的验证记录有一项KeyError，且新增接口需要检查约束冲突和告警是否正确保留。
影响：验证当前False设置及既有True/False行为，评分报告作为既有评估快照而非本轮重新测得的质量保证。

## 文件与修改来源

Antigravity 本地会话的225–241号编辑记录可对应以下9个文件；改动内容已逐项与Git基线比对：

- `AGENTS.md`
- `skills/flight-control/scripts/assemble_plan.py`
- `uav_runtime/atomic/allocation.py`
- `uav_runtime/graph_planner.py`
- `skills/mission-planning/references/artifact-pipeline.md`
- `skills/mission-planning/references/combined-tasks.md`
- `skills/mission-planning/references/plan-delivery.md`
- `skills/mission-planning/references/task-graphs.md`
- `skills/target-detection/SKILL.md`

Codex 按用户要求取消规划前环境自检的7个文件如下；与上述清单重叠2个，共形成14个昨天的源文件差异：

- `AGENTS.md`
- `skills/mission-planning/SKILL.md`
- `skills/mission-planning/references/artifact-pipeline.md`
- `skills/flight-control/references/online-submission.md`
- `skills/target-detection/references/online-perception.md`
- `skills/trajectory-generation/SKILL.md`
- `skills/trajectory-generation/references/offline-preview.md`

`SHOW_ACTIONS=False` 为已存在的本地运行设置，本次与昨日版本同步，不据文件时间单独认定该布尔值由哪个模型更改。原始14个文件在发布准备中未修改运行逻辑；Codex 本轮新增/调整的只有以下3个测试文件及发布材料：

- `tests/test_artifact_pipeline.py`：按本次show_actions检查展示结果，另检查隐藏时完整raw_batch仍保存。
- `tests/test_failure_diagnostics.py`：新增约束合并、交集为空、固定归属/数量冲突、输入不被修改及摘要字段检查。
- `tests/test_task_graph.py`：检查摘要的预算、距离和TIME_RESERVATION告警与完整结果一致。

## 验证与评估资料

发布副本执行49项 runtime 回归及4项 JSON 回归；本地聊天平台另执行2项 fake-bot 回归，共55项。测试只验证已有离线工具与边界，不启动飞行或调用付费模型。

```bash
PYTHONPATH=. python -B -m unittest discover -s tests
PYTHONPATH=.:chat_with_bot python -B -m unittest discover -s chat_with_bot -p test_reply_json.py
```

数据库与其他运行模块保持Git基线内容。校验同时核对发布副本与主工作区的源文件一致性，防止只同步文档而漏掉实现。

- [评分规范（v3.1口径）](UAV打分_v3.md)
- [49题修改前后对比原报告](comparison_before_after_49.md)

评分报告为Antigravity此前生成的既有快照，本轮未重新评分或逐题独立复核；平均分、截断率等数字不作为单次代码变更的因果证明。原始轨迹、API日志、评分JSON、聊天平台凭据/配置和Antigravity会话数据库均保留本地，不随本仓库上传。未改动外部nanobot执行循环，因此不能将既有空正文重试问题表述为底座已修复。
