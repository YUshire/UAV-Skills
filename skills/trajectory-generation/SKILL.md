---
name: trajectory-generation
description: 【单项航迹预览】负责：单项 A→B 距离/航线（query-path）或单区域路径预览（query-area），以及纯飞行/周期单趟只读预览。不负责：普通完整任务（已由 mission-planning 的 --pipeline 自动封装，勿单独读取）。
---

# 航迹入口

| 当前需要 | 只读对应页 |
|---|---|
| 两点距离/航线、单项区域路径，或纯飞行/周期单趟只读预览 | [offline-preview.md](references/offline-preview.md) |
| 手动单独调用 `planning_routes.py`（`costs`/`build`，非 `--pipeline`） | [planning-routes.md](references/planning-routes.md) |

所有坐标为 WGS84，距离/高度为米；本地结果为离线合成几何（未检查现场障碍），不可冒充避障航迹或直接下发。
