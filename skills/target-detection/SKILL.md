---
name: target-detection
description: 【目标清单与感知】负责：带目标搜索的目标清单构造与格式校验（validate_targets），以及单项感知启停/进度查询/发现通知。不负责：无目标探索、纯巡逻、或扫描对象（战壕/道路归 task-graphs）。
---

# 目标清单与感知

> 目标清单构造与校验仅用本页；**仅单项开启/更新/停止识别、查进度或中央下发前启动感知**再读 [online-perception.md](references/online-perception.md)。单项感知不生成飞行动作；无目标探索/建图与纯巡逻**不构造目标清单、不调本脚本**。

## 目标校验 SOP（3 步）

1. **构造目标清单**（同一任务所有搜索参与机共用同一份清单与 `search_id`）：
   ```json
   {"search_id":"<最终航迹或任务图summary返回的task_key>","targets":[
     {"target_id":"red_person","category":"person","description":"穿红色上衣、黑色裤子的人"}
   ]}
   ```
   - `target_id`：清单内唯一，1..80 位 `[A-Za-z0-9_-]`；同类不同外观拆成不同 `target_id`。
   - `category`：1..100 字符非空英文粗类别（常见 COCO 如 `person/car/truck/bicycle/backpack/handbag/suitcase`；非常见目标如头盔、纸箱、购物袋、火源、火炮、热源等填最贴近的简洁英文词，颜色/外观/移动状态全部写入中文 `description`）。
   - **不支持数量去重**：禁止复制多条相同描述的条目来冒充“至少找到 N 个”；不同外观的多目标不受此限。“扫描战壕/道路”属扫描对象，走 `task-graphs.md` 的 `scan` 节点，不塞进 `targets`。
2. **仅对实际承担带目标搜索的执行机调用校验**（不要对纯巡逻机或无目标探索机调用）：
   ```bash
   uav_runtime/.venv/bin/python uav_runtime/run.py skills/target-detection/scripts/validate_targets.py \
     --uav-id <UAV-XXX> --query-json '<目标清单JSON>'
   ```
   - 本脚本**仅校验格式与机号记录，无硬编码类别白名单**；格式报错修正重试 1 次即可。
3. **诚实表述边界**：返回 `validated=true, backend_verified=false` 只证明格式与机队记录通过，不证明后端支持该类别或已启动感知；面向用户说明“目标配置已检查，实际识别能力尚未验证”。

## 目标发现通知与复核语义
- 收到目标发现通知：用 1–2 句话说明哪架机发现了哪个目标，不输出事件 ID、坐标、日志或推理过程，不承诺持续监控。
- 中央复核以逐 `target_id` 的 `MATCH` 为准；YOLO 框仅是候选，`NO_MATCH`/`UNKNOWN` 不证明现场无目标；目标位置缺失不以无人机位置替代。
