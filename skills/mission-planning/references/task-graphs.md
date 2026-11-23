# 复杂任务规划契约

入口：`uav_runtime/run.py skills/mission-planning/scripts/plan_task_graph.py --input-file <JSON>`。
此入口生成完整意图、分支及已知航迹，**只规划**。旧普通任务入口、旧检查器及旧飞控命令不变。

## 输入

顶层 `nodes` 必填，1～64个。`edges` 最多256条；可达分支最多64种；不接收无界循环。
沿用 `flight_alt`（默认1.2m，须>0.3m且满足机型升限）、`speed`、`search_timeout`、`uav_ids`、`exclude_uavs`、`payload`、`constraints`、`solver_options`。`max_duration_s` 可设置全任务总时限。

节点格式：`{id, action, uav_id?, job?, params?, join?}`。

| 字段 | 含义 |
|---|---|
| `id` | 唯一编号，字母开头，仅字母数字、下划线和连字符 |
| `action` | 区域动作search/patrol；接口动作record_start、record_stop、photo、set_mode、scan、track、observe、hover、wait、report、transit、return、land |
| `job` | 仅search/patrol。沿用resolve_jobs的地点、中心、形状、尺寸、laps、search_timeout；编号、机号及类型从节点取得 |
| `uav_id` | 用户明确指定的执行机。几何作业可留空由原分配器选择；接口步骤仅在前置执行机唯一时继承，否则保留待明确 |
| `params` | duration_s（正秒数）、mode、target、targets、description、until_step。未支持的字段报错，不静默忽略 |
| `join` | 默认all：全部前置条件满足才启动；any：任一前置条件满足即可启动 |

依赖格式：`{from, to, on?}`；on默认为completed，也可为found、not_found、timeout。后三种只来自search节点，互斥，不混同。未连接的不同机步骤可并行；同机飞行步骤按稳定拓扑顺序串行，输出resource_edges说明资源顺序。这是可行的保守编排，不保证调度最优。公共几何步骤首次选定单机后，在其他结果分支固定该机；共担机组若仍无法保持一致，标记待联合分配，不声称完整条件方案已验证。

一个search节点的found事件作用于该节点的目标集合；若用户要求“搜索A、发现B后跟踪B”，把A、B都列入该节点目标，并在track节点明确`target=B`、回复写明“发现B后跟踪B”。当前不支持per-target的found分支；不要为这个语义反复推断，也不要把A的发现当成B的发现。

录像与模式按预留能力可用处理。record_start为开启动作，可通过params.until_step指定结束步骤，也可配显式record_stop；持续录像本身不阻塞飞行。track/observe/hover/wait/scan要保留用户时长；缺少必要时长时保留待补，不用默认profile覆盖。

## 示例：搜索后录像；未找到时改搜另一区

```json
{
  "flight_alt": 100,
  "nodes": [
    {"id":"S1","action":"search","uav_id":"UAV-016","job":{"center_lon":121.442057,"center_lat":25.239462,"shape":"rectangle","length":12,"width":8,"search_timeout":60},"params":{"targets":["灰色背包"]}},
    {"id":"V1","action":"record_start","uav_id":"UAV-016","params":{"target":{"from_step":"S1","field":"target_position"},"until_step":"V2"}},
    {"id":"V2","action":"wait","uav_id":"UAV-016","params":{"duration_s":30}},
    {"id":"S2","action":"search","uav_id":"UAV-016","job":{"center_lon":121.4421,"center_lat":25.2395,"shape":"circle","radius":5,"search_timeout":60}}
  ],
  "edges": [
    {"from":"S1","to":"V1","on":"found"},
    {"from":"V1","to":"V2"},
    {"from":"S1","to":"S2","on":"not_found"}
  ]
}
```

发现位置周围搜索：job可用`"center_from":{"from_step":"S1","field":"target_position"}`，同时给shape与尺寸。该节点必须受S1的found分支约束。未知位置不能用原区域中心代替。

已有结果可传：
`"observed_events":{"S1":"found"}, "runtime_bindings":{"S1":{"target_position":{"longitude":121.4421,"latitude":25.2395}}}`。
此时可补算从登记起飞点出发的完整分支预览；在线触发后的增量执行仍须用最新真实位置与状态重新规划，不能把此快照预览当成实时航迹。

## 示例：扫描对象 + 检测目标 + 跟踪

用户要求“扫描战壕，发现移动人员后跟踪”时，不要把“战壕”塞进 COCO 目标清单；扫描对象放在扫描步骤，检测/跟踪对象放 `targets`：

```json
{
  "flight_alt": 100,
  "payload": ["激光雷达"],
  "nodes": [
    {"id":"S1","action":"search","job":{"center_lon":120.4485,"center_lat":23.485625,
     "shape":"rectangle","length":100,"width":80,"search_timeout":1200},
     "params":{"targets":["moving_person"],"description":"激光雷达扫描战壕"}},
    {"id":"SC1","action":"scan","params":{"targets":["战壕"],"description":"激光雷达扫描战壕"}},
    {"id":"T1","action":"track","params":{"target":"moving_person"}}
  ],
  "edges": [
    {"from":"S1","to":"SC1"},
    {"from":"S1","to":"T1","on":"found"}
  ]
}
```

`scan` 是接口步骤，沿用前置 `S1` 的区域和执行机，不重复计算几何；扫描对象放 `SC1.params.targets`/`description`，检测/跟踪对象放 `S1.params.targets` 和 `T1.params.target`。若后端尚未实现 scan/track，保留节点并标记 pending/待验证，不删除扫描或跟踪要求；离线只交付几何前段。

这个示例只表达“区域几何 + 扫描接口 + 跟踪接口”的编排，**不证明扫描过程中同时检测或立即中断跟踪**；`S1→SC1` 是前置/接口关系。用户要求“扫描中检测/立即跟踪”时，把该同时性作为未验证接口条件保留，不能声称本示例已经满足。

- 明确的 completed 依赖无论是否同一架机都保留；只有没有依赖的步骤可并行。“先…再…”同机用 completed；用户明确写跨机“完成后”也用跨机 completed。因航程/半径/载荷必须换机或改并行时，回复必须写“顺序改变”并取得接受，不能一边改并行一边说原顺序已保留。
- 用户要求严格统一航高 X 米时，当前多机错高会生成 X.3 米且没有关闭错高的工具开关；不要手工改生成高度，也不要声称已满足。说明冲突并询问是否接受 0.3 米错高或改单机/分任务，严格 X 作为未满足项保留。

## 计算范围与回复

1. 每条可达分支单独调用原prepare→resolve→costs→allocate→build→assemble，互斥分支不同时执行、不叠加预算。
2. 保持单机几何作业顺序，保留跨机completed依赖。后机可等前机完成再起飞；已经起飞的同机后续等待计入时间。
3. 未知位置/跟踪路径/中途落地改变后续原点时，只计算已知连通前段；其他步骤仍保存在图中，标记pending。接口可规划与航迹尚待计算是两个状态。
4. 时间按各搜索的完整时限与几何估计取大值，并增加按max_up_speed/max_down_speed估计的起降、显式时长和等待。该数值是规划预留，不是现场完成保证。几何航程、电量仍沿用原模型，完整能耗认证保持false。
5. 任务图的几何预览默认带原有备用返航降落段；return/land是图中结束要求，不能把预览段提前执行或与图中结束动作重复执行。

顶层status=OK只表示规划器正常返回。看planning_status：PLANNED已生成、CONDITIONAL存在条件/运行时待定、NEEDS_DATA缺必要数据、INFEASIBLE有分支约束不通过、BLOCKED工具或旧动作接口阻塞。每个分支均有对应状态、violations和pending。

仅搜索时限等时间预留超预算时标记CONDITIONAL，不能据此断言任务物理不可行；只有明确持续时间等必要下界已超限才作时长冲突。默认搜索时限造成不足时，缩短时限属于另算的备选，不自动改写原输入。原动作校验失败时仍保留已生成的几何航迹与分配证据，标记BLOCKED。

回复逐项写清执行机、对象/区域、先后/并行、触发条件、时长和结束动作；列出改变原要求的备选差异。`reply`是包含全部步骤的基础回复，需结合本次scenarios的实际分配、区域和预算补充。即使分支不可行，也不能只写失败原因；必须保留所有节点，包括track的触发条件、时长（未给写待补）和结束动作。默认参数下失败时，按 [阶段诊断](diagnostics.md) 判断并报告条件备选。无论几何预览是否通过，均不宣称已经飞行或已完成录像等动作。

此入口不认证物理目标去重、整区覆盖、不丢失目标或真实模式成像效果。保留这些要求并说明尚待验证，不能把“接口假定可用”扩大解释成效果已保证。

跨分支错高采用固定序列：先按机号排列全图已知指定机，首次自动选定的其他机依次追加；基准值上每名增加0.3米，各自检查升限。这样动态分支尚未取得目标位置时，已知公共前段也不会在后续分支重新编号变高。结果planning_bindings记录该序列和已固定归属。旧build不提供altitude_order时仍按原来的参与机编号排序。

## 解释与执行边界

- 先把完整原话整理为节点和依赖；扫描对象与检测目标一次分类，不反复增删目标。录像、高清、红外等预留接口保留并按可用规划，执行机的登记载荷限制仍单独核对；`validate_targets` 的 `backend_verified=false` 仅表示格式/记录检查，不证明类别受后端支持，也不据此删除接口。
- “找到后以它为中心……”使用运行时目标位置引用，不要求用户预先提供未来坐标；“确认找到”是发现条件，不是再次确认执行意图。保留后续节点，未知位置先交付条件方案及已知连通前段，触发后用真实位置和最新机况补算。
- 有界扩搜、接力或重复先展开为有限节点并保留终止条件；无界“直到找到”不能静默改为一遍搜索。
- 用户说“航高保持 X 米”且未限定步骤时，理解为作业与转场定高（不含起降）；除多机错高外，还须核对 [flight-control](../../flight-control/SKILL.md) 的 `fuel_explore` 高度框边界，不能把高度框当作全程定高。
- 旧 `check_task.py` 的 `CROSS_UAV_ORDER` 不代表任务图不能规划。此入口只规划，不下发；不得把条件图/接口节点拼进旧 queue_batch，也不得将 `geometry_actions` 已知前段直接下发为整任务。
- 离线交付保留完整原图作为目标及策略证据；普通链路另按 flight-control 保存目标/策略关联产物。规划结果不是策略已经执行的证据。交付通用格式及动作展示按 [交付规范](plan-delivery.md)。

涉及候选机角色、点名数量、相对基准或区域补齐时，按需查 [普通规划的意图、区域与候选](artifact-pipeline.md#意图区域与候选)；只补当前缺项，不重复已完成的普通计算。
