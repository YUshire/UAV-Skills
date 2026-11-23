---
name: target-detection
description: 管理每架无人机的目标感知：把用户要找的目标整理成目标清单，启动、更新、查询和停止感知会话，查询多目标搜索进度，并提供中央任务注册所需的感知引用。用于带目标的搜索任务下发前的感知准备，以及“开启/停止识别”“还剩哪些目标没找到”这类单项请求。
---

# 目标感知

目标清单及离线校验按本页；**仅开启/更新/停止识别、查询进度或在线下发前**读 [online-perception.md](references/online-perception.md)。单项感知不生成飞行动作，只规划不启动感知。


## 目标清单

用户要找的每个目标拆成一项。同一任务的所有参与机共用同一份清单和同一个 `search_id`：

```json
{"search_id":"<task_key>","targets":[
  {"target_id":"red_person","category":"person","description":"穿红色上衣、黑色裤子的人"},
  {"target_id":"pink_case","category":"suitcase","description":"浅粉色、贴有四张贴纸的行李箱"}]}
```

| 字段 | 规则 |
|---|---|
| `search_id` | 本任务唯一，用最终航迹结果的 `task_key`；目标清单改动时换新 ID |
| `target_id` | 清单内唯一，用英文小写和下划线概括目标，如 `red_person`、`black_backpack`；不超过 80 个字符 |
| `category` | 检测模型的英文粗类别（COCO 类名）：人→`person`，汽车→`car`，卡车→`truck`，自行车→`bicycle`，背包/双肩包→`backpack`，手提包→`handbag`，行李箱→`suitcase` |
| `description` | 目标的具体外观，可以用中文，供中央语义复核使用 |

- 同一类别但外观不同的目标，拆成不同的 `target_id`，不合并。
- `targets` 表达按特征匹配的对象，不是已去重的物理实例计数。多个 ID 使用同一类别和同一描述，不能证明找到了多个不同实体；不得用复制条目来表示“至少N辆/人/件才完成”。当前不支持这类去重数量完成条件，需明确说明，不能以目标配置 dry-run 通过替代它。不同外观目标的多目标搜索不受此限制。
- 颜色、衣着写在 `description`，不写进 `category`（不要写成 `red person`）。
- 中央任务 `authorize` 的 `targets` 使用同一份清单（只取 `targets` 数组）。
- 探索、建图任务没有目标，不启动感知。
- “查看、看看、扫一遍”结合当前请求解析；上下文有搜索对象就沿用，否则按无目标区域查看处理。已给出的可识别类别或外观足以表达目标时直接整理清单，不追问额外特征。

## 扫描对象与检测目标

- “扫描战壕”“沿路扫描”中的战壕、道路是**扫描对象**，放在任务图 `scan` 步骤（`params.targets`/`description`）或扫描要求里，不放进 COCO `targets`。
- `targets` 只放检测/跟踪对象，例如“移动人员”；`category` 仍用 COCO 类名。
- 设计好的扫描/识别/跟踪接口默认保留并按可用规划，后续补充实现；不因 `backend_verified=false` 或能力未验证拒绝任务。
- 目标清单不替代扫描要求；扫描对象不要用 target 条目表示。



## 本地离线校验

本地离线校验：不需要 ROS 或外部运行项目，命令为

```bash
uav_runtime/.venv/bin/python uav_runtime/run.py skills/target-detection/scripts/validate_targets.py \
  --uav-id <uav_id> --query-json '<含search_id和targets的目标清单对象>'
```

返回 `validated=true`、`submitted=false`、`perception_started=false`、`backend_verified=false`；只证明目标格式和机队记录通过，不证明类别受后端支持、识别效果、感知已启动或目标已找到。不得用试填类别推断能力；面向用户说“目标配置已检查，实际识别能力尚未验证”。

要求发现后跟踪时，按 flight-control 的离线交付规则保存目标与拟用策略，并说明跟踪尚待服务接通验证；不把目标格式校验当作跟踪能力验证。策略字段留在方案文件中，普通回复无需罗列内部字段。

目标格式报错且修正明确时重试一次；其他失败停止依赖步骤，在线会话错误仅涉及在线操作时读参考页。

## 发现通知的回复

收到目标发现通知时，用一两句话说明哪架机发现了哪个目标；不输出事件 ID、坐标、日志或推理过程，也不承诺持续监控。感知不可用的通知同样用一句话说明。

## 结果语义

- 目标是否找到以中央复核为准：逐个 `target_id` 看 MATCH。YOLO 框只是候选；NO_MATCH、UNKNOWN 都不等于目标不存在。
- 检测不到粗类别时不会触发语义复核，不能据此断言现场没有目标。
- 目标位置缺失时如实说明，不用无人机位置代替目标位置。
- “行驶中”“车队”等不是检测模型的完成条件：目标按粗类别逐项匹配，移动状态只写在 `description` 里供语义复核；不要承诺找到“一个车队”或统计车队数量。
