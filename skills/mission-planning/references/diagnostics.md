# 阶段诊断

仅在当前阶段失败或工具返回条件备选时，读取对应小节；成功路径不预读整页。保留本次 status、message、stage 和证据；停止依赖失败结果的步骤，可独立部分继续。用户回复用日常语言说明已完成范围和具体阻塞。

失败须区分：缺信息、默认参数不足、当前候选池性能限制、接口能力不足和工具异常。缺信息不等于不可达，默认失败不等于物理无解，局部候选失败不代表全机队无解。备选改变明确要求时保留原要求并标为待接受，包括减少点名机、改共担方式、改速或改航高；离线交付也相同。

## 参数与成本

指定速度先核对逐机 `max_speed`；超限说明冲突并只询问冲突参数，不自动限速。只咨询时可展示待接受的限速备选，不能称原速度已满足。

默认参数可能造成“参数性不可行”，必须和“物理不可行”分开：

- 成本筛查的 `candidate_screening.alternatives` 会列出“按逐机 `cruise_speed` 规划仍失败、但在不超过该机 `max_speed` 的全局速度下可完成对应作业”的候选。存在时，不要判成全部不可达；按“按巡航速度规划不可达；UAV-X 需不低于 Y m/s（上限 Z m/s）可完成对应作业”说明，并把提高速度标为待用户接受的备选。用户没有要求速度时，不把备选速度直接当成已采用值。
- 搜索覆盖在默认航高下超航程/时间时，先看 `candidate_screening.altitude_alternatives`（或把同一任务的 `flight_alt` 提高到 2/3/5 m 重算一次）；说明“本次默认航高不可行；X m 下 UAV-Y 可完成，预计…”，并标为条件方案。提高航高可能降低识别分辨率，需设备规格/实测确认，不直接承诺能识别目标。
- 航高按同样方式处理：如果候选都因 `max_altitude` 低于请求航高被排除，错误会带 `max_recorded_altitude`；说明“请求 X m 不可行，记录中的最高升限为 Y m”，把降低航高标为待接受备选，不直接改用户明确航高。
- 指定全局速度时，成本工具会按 `max_speed` 自动以 `SPEED_LIMIT` 排除不支持该速度的候选，不需要手工缩小候选范围。降低速度或改用另一机型属于改变用户明确参数，须标明为备选并取得接受。

诊断读取顺序：本次 `candidate_screening.diagnosis` → `nearest`/`excluded` 的逐机距离与限制 → `alternatives`/`altitude_alternatives`。`RANGE_LOWER_BOUND` 是航程下界；`TIME_LOWER_BOUND` 是规划速度下时间不足；`BOTH_LOWER_BOUNDS` 两项都不足；`FLIGHT_RADIUS` 是最远作业点超过机型半径，提高速度不能解决；`SPEED_LIMIT` 是指定速度超机型上限。保留对象、单位与统计范围，不笼统归为航程不足。

有速度/航高备选时同时交付原参数失败及有证据的条件方案；两类备选均为零时，只能按返回证据描述当前候选池的限制，不能把有限探测当成遍历所有允许参数的物理无解证明。全局速度不是逐机最大速度估时；使用本次实际采用参数，不沿用示例或历史值。

候选准备的 `max_recorded_altitude` 用于说明请求高度与登记升限冲突；坐标转换保护范围动态读取返回的 `coordinate_filter.max_distance_m`，不是物理航程/机型半径。合并候选准备和成本的排除证据，不能把剩余池预算失败推广为全机队物理无解。

### 成本筛查字段（需要展开工具证据时）

`candidate_screening` 包含：`reasons`（`RANGE_LOWER_BOUND` / `TIME_LOWER_BOUND` / `BOTH_LOWER_BOUNDS` / `SPEED_LIMIT` / `FLIGHT_RADIUS` 计数）、`nearest`（最近5架的航程/时间/半径是否通过，以及巡航速度下的时间和 `max_speed_mps`）、`range_ok_but_time_fail`（航程够但规划速度下时间不够的机号）、`excluded`（最多20条逐机依据，其余以 `excluded_omitted` 计数）、`assumed_transit_speed_mps` / `transit_speed_mps` / `work_speed_mps`（规划速度范围）和 `reachable_count` / `cost_excluded_count`。指定 `speed` 时，`max_speed` 低于该速度的候选自动以 `SPEED_LIMIT` 排除，不需要手工缩小候选范围。所有候选被筛掉时另有 `diagnosis`，按本页绑定约束解释。`alternatives` 给出按巡航速度规划失败、但指定不超过 `max_speed` 的全局速度后可完成对应作业的候选（`feasible_count`、`items`、`required_speed_mps`、`suggested_speed_mps`、`max_speed_mps`、`binding`）。单作业搜索在默认航高下不可行时另有 `altitude_alternatives`（`default_altitude_m`、`items[].altitude_m`、`total_range_m`、`total_time_s`、`battery_after`）。航高备选的采用与识别能力边界按本页前述规则处理。

## 数值分配

`NO_FEASIBLE_ALLOCATION` 仅表示给定候选、成本、约束下无解。读本次诊断：`SHARED_JOB_TIME_BUDGET` 给出强制共担的等待、返航时间下界和预算；`UNDETERMINED` 表示尚未定位具体原因，不猜测。各机单独往返可行不证明共担可行。保留 `required_drones`、固定归属、允许集合及分区要求，不删约束或手拆作业绕过。

求解超时、失败或不可用是运行异常，不是已证明物理不可行；字段/成本/预算不一致则按原入口错误修正，不将失败结果当有效分配。

## 动作组装与校验

本次失败结果的 `participants` 给出逐机状态和原因。整体失败且无动作产物时，区分已生成几何航迹、检查通过与失败的机；不得把保存请求或部分方案称完整动作通过。无需复制组装代码逐机重算，修复失败项并重新组装通过后才有完整动作方案。

`ALLOCATION_EVIDENCE_MISMATCH` 是分配与成本/求解证据不对应；`ALTITUDE_INFEASIBLE` 是错高超过限制；`BUDGET_EXCEEDED` 是最终几何超过预算。不手改归属、航点或错高求通过；明确严格同高要求未满足时保留冲突。

坐标超限使用本次转换器返回的阈值和逐机原点说明，不写死公里数，也不把转换保护范围称物理动作半径。后续执行条件保留尚未修复的动作检查失败，不能只写接通服务即可下发。动作展示仍按 [flight-control](../../flight-control/SKILL.md) 唯一布尔开关。

## 工具与产物引用

`INVALID_REFERENCE`：核对本次上游实际返回路径及 `/data` 字段，明确可修正时重试一次；仍失败则停止依赖步骤。不遍历目录、通配猜文件或借旧产物冒充成功。确定性环境缺失遵循 AGENTS.md，不重试同一失败入口。

一次有工具依据的航高或速度备选后停止；不重读求解器源码、重复同一 costs、复制组装代码补诊断或反复重写回复。没有新证据时按已有结论交付。下发/运行失败只在涉及该阶段时读 [flight-control](../../flight-control/SKILL.md) 或 [运行控制](../../flight-control/references/runtime-control.md)。
