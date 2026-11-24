---
name: trajectory-generation
description: 使用本地离线几何计算单项航迹、转场与作业成本，并按求解归属和顺序生成每机航段、区域分工及航程估计。结果标记为mock，不校验地图障碍。
---

# 航迹入口

| 当前需要 | 再读什么 |
|---|---|
| A 到 B 的距离/航线，或单项区域路径预览 | [offline-preview.md](references/offline-preview.md)：query-path/query-area 参数与结果 |
| 完整任务的区域成本，或按已有分配生成每机航段 | [planning-routes.md](references/planning-routes.md)：costs/build 契约；两者均不执行分配算法 |
| 成本、预算或候选失败 | 仅读已有 [阶段诊断](../mission-planning/references/diagnostics.md) 的对应小节 |
| 跨机依赖、条件分支或动态目标位置 | [任务图](../mission-planning/references/task-graphs.md)，不能用普通顺序流水线替代 |

只读本次命中的模式；完整方案不额外调用单项预览拼接航迹。普通连续计算可复用 [产物串联](../mission-planning/references/artifact-pipeline.md) 与实际 ref/select，不手抄矩阵或猜路径。

所有经纬度为 WGS84，距离/尺寸/高度为米。当前本地结果是 mock 几何，未检查现场障碍；coverage 及格式通过不证明整区覆盖或实际执行。只在已有部署信息表明外部语义地图可用时使用其入口，具体错误见预览参考页；不能拿离线结果冒充避障航迹。
