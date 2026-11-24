# 本地计算产物串联

`uav_runtime/.venv/bin/python uav_runtime/run.py --pipeline --input-file <本次唯一输入JSON路径>`，也可用 `--input '<JSON对象>'`。

顶层为 `steps` 数组，每步包含唯一 `id`、脚本相对路径 `script`、该脚本原有的 `input`。支持 prepare_candidates、resolve_jobs、planning_routes、allocate、assemble_plan 五个原数值脚本，按数组顺序执行。单步命令不变，不包括在线下发。

字段引用写为 `{"ref_step":"上游步骤id","select":"/data/字段"}`，只能引用本次前面成功的步骤；省略 select 时取 `/data`。程序从上游返回 JSON 读取实际 `artifact_file`，转成原有 ref/select，不搜索目录、不选择旧文件。原有明确的 ref/select 仍可用于本次已获得的输入产物。`allocate` 步骤在引用 `costs` 约束的同时需要叠加用户额外约束（如 `allowed_assignments`、`required_drones`、`fixed_assignments`、`single_owner_jobs`）时，直接写入同级字段 `"extra_constraints": {...}`，由工具自动合并，无需拆分 pipeline 或翻看源码。`resolve_jobs` 的每项作业仅接收字段：`job_id, name, place, center_lon, center_lat, shape, radius, length, width, task_type, laps, search_timeout, uav_id`。

以下示例输入已明确的区域，选择指定机，串联到完整动作；区域、机号、航高和约束均按用户任务填写，不照搬示例值。机况等参数仍按各技能核对。

```json
{
  "steps": [
    {"id":"fleet","script":"skills/data-query/scripts/prepare_candidates.py","input":{"uav_ids":["UAV-016"],"flight_alt":1.2}},
    {"id":"regions","script":"skills/spatial-compute/scripts/resolve_jobs.py","input":{"jobs":[{"job_id":"J1","center_lon":121.442057,"center_lat":25.239462,"shape":"circle","radius":5,"task_type":"patrol"}]}},
    {"id":"costs","script":"skills/trajectory-generation/scripts/planning_routes.py","input":{"operation":"costs","candidates":{"ref_step":"fleet","select":"/data/candidates"},"jobs":{"ref_step":"regions","select":"/data/jobs"},"flight_alt":1.2}},
    {"id":"allocation","script":"skills/mission-orchestration/scripts/allocate.py","input":{"candidates":{"ref_step":"costs","select":"/data/candidates"},"jobs":{"ref_step":"costs","select":"/data/jobs"},"costs":{"ref_step":"costs","select":"/data/costs"},"constraints":{"ref_step":"costs","select":"/data/constraints"}}},
    {"id":"routes","script":"skills/trajectory-generation/scripts/planning_routes.py","input":{"operation":"build","cost_estimate":{"ref_step":"costs"},"allocation":{"ref_step":"allocation"}}},
    {"id":"actions","script":"skills/flight-control/scripts/assemble_plan.py","input":{"plan":{"ref_step":"routes"}}}
  ]
}
```

返回保留最后一步的原有字段与动作摘要，另有 `pipeline_steps` 列出本次已成功步骤的产物路径。某步失败立即停止，保留原错误及诊断，`failed_step` 指出失败步骤；已成功前段不表示整个任务完成。参数备选仍遵循用户约束和原技能的重试规则。

此入口不表达发现/未发现/超时条件，也不负责运行时位置绑定。此类任务使用 [任务图入口](task-graphs.md)，保留完整条件方案及待运行时补算的节点。

## 意图、区域与候选

**先理解、查库补齐，再继续规划。** 用户无需按接口字段描述任务。按整句话及上下文确定动作、地点、对象和顺序；已给信息直接沿用，能查到的地点、区域、机况和资源直接采用，未指定的常规参数使用本节默认值。只有查询后仍缺必要事实，或不同解释会实质改变任务且上下文无法判断时，才询问最少缺项。

**日常表达**：

| 表达 | 理解与处理 |
|---|---|
| 单独的“去A”“先去A，再去B” | 到达或途经A，保留先后顺序；“去A看看/巡逻”整体归入该地点的探索/巡逻任务 |
| “去看看”“查看某区域”“搜索某地”“搜一下某地”“扫一遍” | 上下文有目标则沿用目标；没有目标则按无目标区域查看/探索，不追问“要找什么” |
| “找红衣服的人”“搜那辆车” | 带目标搜索；从原话和上下文提取已有对象及特征 |
| “沿边走一圈”“绕着巡两圈” | 边界巡逻，保留圈数；范围优先用用户描述，否则用地点库登记区域 |
| “覆盖巡查”“搜完整个范围，不只飞边界” | 未指定目标时可按无目标探索，使用 `task_type="search"` 计算候选方案；不据此推断用户指定了固定往返航线。当前不能保证整区覆盖，须明确该要求尚未满足；用户接受此差异后才可下发 |
| “某地周围N米扫一遍”“某地周围N米的圈巡两圈” | 按地点区域中心（仅有点位时用该点）、半径N米整理，保留动作和圈数；明确说距建筑边界外扩时保留外扩含义 |
| “把某区域分成两块，两机各搜一块”“两机分区搜索” | 一个搜索作业、两机共担：`allowed_assignments` 列出两机，`required_drones=2`；不手工拆成两个整区作业，分区由 build 自动完成（不能指定哪架飞哪块） |
| “先……再……接着……最后……” | 明确的执行顺序，保留到任务表示和能力核对中 |

这些是意图解析规则；具体航迹、范围和完成条件仍由相应工具核对。明确要求固定往返航线或整区覆盖时保留该要求。

| 条件 | 判断标准 | 仍需解决的情况 |
|---|---|---|
| 任务意图 | 按上述日常表达区分带目标搜索、无目标探索/建图、巡逻和单纯飞行 | 仅说“处理一下”且无上下文，无法确定动作 |
| 任务地点 | 地名、经纬度或相对位置；只有方向+距离时，按下述“相对基准”区分指定单机与地点库默认位置 | “去附近搜索” |
| 区域 | 只支持圆形和矩形；用户给的形状、尺寸优先，否则直接使用地点库登记的完整区域 | 查库后仍只有点位且无范围；用户指定圆形但上下文和记录均无适用半径 |
| 相对方位 | 东西南北等绝对方位可计算；左、右、前、后从上下文或可用状态取得朝向 | 仍无朝向时才询问，不能将“左前方”固定解释为西北 |
| 参数合理 | 任务航高 > 0.3 m，且不超过参与机型升限；速度、半径、长宽 > 0；圈数为正整数；经纬度合法 | “速度 -5 m/s”；“超过所选机型升限的航高” |
| 指令无冲突 | 各项要求不互相矛盾 | “用最大高度 1 米的机，航高 2 米” |
| 完成条件与能力 | 区分任务意图、完成条件、结束动作；工具成功还须能表达用户明确的条件 | 数量去重和整区覆盖认证等不能靠格式校验变成已支持；复杂条件与时长要求须进入任务图 |

- 数值超出工具或机型范围时，指出具体冲突并只询问冲突参数。工具中搜索和探索均用几何标签 `task_type="search"`，是否配置目标取决于用户语义。
- “可疑目标”先结合当前任务和历史描述解析；仍没有可识别对象或判定特征时，再询问。
- 巡逻按区域边界飞闭合环。边界不可通行时，先检查用户已给或库中登记的适用外围范围；仍无可用范围再询问。
- 只有用户明确限定固定往返航线时，才按当前执行入口不支持该路线约束处理，说明具体缺口并可交付只读预览；改变为边界巡逻或自主探索需用户接受。仅要求内部覆盖时，可先计算无目标探索候选方案，不必先追问任务名称；但必须说明整区覆盖要求尚未满足，接受该差异后才可下发，不能把探索方案当作原要求已满足。
- 只有用户明确说“跟踪”“跟随”时才启用跟踪。
- 用户明确要求载荷（红外、激光雷达等）时才按载荷筛选；普通目视搜索不加载荷条件。

**相对基准**：沿用用户或会话已明确的参照。只有方向+距离且明确指定单架执行机时，使用该机有来源的当前位置；状态库坐标只是起飞点记录，不能冒充实时位置。用户已指定登记起飞点或会话已采用该基准时直接使用；确需当前位置而工具无法取得时，才询问位置或可否使用起飞点。未指定执行机时用地点库“默认位置”；多机共同参照先从上下文解析，仍无法唯一确定再询问。

指定速度需核对参与机资源记录的 `max_speed`；超限时读 [参数诊断](diagnostics.md#参数与成本)，不静默改速。成本或 dry-run 成功不证明机型速度限制已核对。

历史复搜先沿用会话中的目标特征，缺少时查询可访问的历史记录；两者均无法取得才询问。离线入口已说明不支持某种历史查询时，不换子命令或遍历不可读目录反复尝试同一能力。

**先核对明确的完成条件**：
- 周期巡逻需要定时启动、间隔等待或跨趟补能调度，当前任务入口未提供；`laps` 仅表示同一次任务内连续绕行。保留单趟圈数、间隔和持续时间，直接说明调度缺口，完成可独立进行的地点、机况查询及单趟只读预览；预览标为周期任务的一部分，执行替代方案需用户接受。
- “找目标，找到后仍把整区搜完/建图”是搜索的完成条件，不因“建图”二字就判成两种联合任务。当前 `ALL_TARGETS_FOUND` 按 [运行控制](../../flight-control/references/runtime-control.md) 的决策规则返航，且覆盖航迹不认证整区覆盖；不能承诺这一条件已实现。说明缺口并保留原要求，不要求用户在已经明确的意图间重新二选一。
- “至少找出N个相同特征的对象”需要物理实体去重计数，不能复制同一描述N次作为N个目标。当前目标清单不保证这项数量条件；按 [target-detection](../../target-detection/SKILL.md) 说明边界。不同外观的同类目标仍可分别搜索。
- “必须保证含往返、起降的总时长”与每区 `search_timeout` 不同；当前水平成本与预算不能提供全任务保证。不能把巡航速度下的估计超限说成所有允许参数下都物理无解，也不能承诺提高速度后必然满足总时限。
- 用户指定跟踪时长时，在任务图 track 节点保留 duration_s，按预留跟踪接口规划；不能把搜索时限当成跟踪时长。旧中央 profile 仍不支持自定义时，不能用旧 profile 下发后宣称已满足。
- 纯飞行/多途经点尚未接入当前完整任务入口和中央作业契约；直接说明接口缺口，并用单项航迹工具完成可支持的路径预览，不追问“到了以后做什么”。混合任务类型和同一架机内的步骤顺序已支持；固定到不同无人机的步骤先后顺序用新增任务图规划入口表达；原 check_task.py 的 gaps 仍代表旧扁平动作入口的边界。不通过拆单返航、改任务类型或重排顺序替代原请求。

**候选机**：
- 按语义识别机号的角色：明确派遣的机号或候选列表才限定执行机；用作位置参照、查询对象或资源说明的机号不等于执行机。例如“在 UAV-002 左前方探索”中的 UAV-002 只是位置参照。
- 没点名、没说具体架数（“派无人机去搜”），或只是说明资源（“我有五架”）：全部合格机都是候选，由算法决定出动几架，不必追问。
- 要求出动架数（“派两架去搜”）：在数值分配中保留 `constraints.required_drones`，表示恰好这么多架实际作业，不能用待命机凑数。候选不足或约束无解时说明原因，保持数量要求。
- 多机共担无解时，保留原数量、归属和分区要求；共担还受作业时段重叠及等待时间预算约束，不能仅凭各机单独往返可行就判定组合可行。依据本次工具证据说明绑定约束，不删除 `required_drones` 或手工拆作业绕过约束；改变共担方式的备选须与原请求分开并标明待接受。
- 某作业限定“从这些机中选择”时使用 `constraints.allowed_assignments`；明确某机独占某作业时使用 `fixed_assignments`。允许集合不等于出动数量，不用额外指定数量来代替它。
- 点名机不可达或不可用时，仍用全量候选查询给出可替代的机号，不能拿一次 `LIMIT` 样本概括“整个机队都到不了”；`CANDIDATE_EXCLUDED` 会返回 `available_candidates` 供参考。判断可行性要同时看转场往返和作业覆盖/巡逻航程；转场可达不等于任务可行。未点名时把作业中心传给 `prepare_candidates` 做附近预筛；不要用最近 K 架替代可行筛选。

**默认值**：航高 1.2 m；巡逻 1 圈；搜索时限 1200 s；任务结束后返航并降落（用户明确要求结束后不返航时，改为原地悬停等待指令）；速度不指定时用逐机 `cruise_speed`，`max_speed` 只做上限。 `flight_remain` 已含健康度、电量与载荷，预算仅扣返航预留，不重复折减。

只有候选、成本或预算失败时，读 [阶段诊断](diagnostics.md)，区分默认参数不足与物理限制。

用户要求作业后悬停时，按 [flight-control](../../flight-control/SKILL.md) 的 HOLD 策略与备用收尾段处理；带目标时 ALL_TARGETS_FOUND 是不同条件，不能用 HOLD 改写其返航规则。

## 按缺项补齐普通完整方案

| 当前缺少的内容 | 使用已有技能 | 返回内容 |
|---|---|---|
| 地点、状态、资源事实 | data-query 原SQL查询 | 真实记录 |
| 合格候选机与预算 | data-query 的 prepare_candidates.py | candidates、excluded |
| 明确的数值区域 | spatial-compute 的 resolve_jobs.py；相对位置先用 calc_offset.py | jobs |
| 多步骤能力核对 | mission-planning 的 check_task.py | 类型/参数/顺序检查、gaps和route |
| 作业与转场成本 | trajectory-generation 的 planning_routes.py，operation=costs | candidates、jobs、costs、constraints及prepared |
| 无人机归属和访问顺序 | mission-orchestration 的 allocate.py | assignments、owners、unused及求解证据 |
| 分配后的具体航迹和航高 | trajectory-generation 的 planning_routes.py，operation=build | 原始WGS84航点、每机steps、航程估计 |
| 动作序列与格式校验 | flight-control 的 assemble_plan.py | 完整动作产物、dry-run结果、逐机报告摘要 |
| 目标配置校验 | target-detection 的 validate_targets.py | 目标清单格式、机号记录校验结果 |

成本输入接收候选和数值区域；分配只接收候选预算、作业标识、成本及明确约束。复用成本结果的 `constraints` 保留固定归属和圆形巡逻的单机要求；圆形搜索可由两机共担，不套用巡逻的单机约束。用户另有逐作业候选集合或数量要求时一并保留，不额外增加用户未要求的出动数量。最终航迹接收同一次 `cost_estimate` 和 `allocation`，不能把归属结果当作航点结果使用。只要已有兼容的数值输入，分配咨询可以直接求解，不必查询地点或生成航迹。

完整只规划方案需按 flight-control 对每架机做dry-run；有目标时按 target-detection 做本地目标校验，保存完整动作和各项验证范围，面向用户按 [交付规范](plan-delivery.md) 交付。在线环境齐全的执行请求使用组装好的原始 WGS84 动作，继续 [flight-control](../../flight-control/SKILL.md) 中央下发流程；只读计算和待确认方案不启动感知。

普通任务连续计算优先使用本文开头的产物串联入口，以步骤名引用上游产物，由程序填入实际路径；单项调用保持不变。引用失败时读 [产物引用诊断](diagnostics.md#工具与产物引用)。条件任务仍使用任务图，不把“没找到才转区”压成普通的 `ordered=true`。

**整理作业**：一个动作步骤一个作业；同一区域的不同动作拆成多个作业。
- 每步分别写 `task_type`（`search`/`patrol`）、`laps`（只巡逻）、`search_timeout`（只搜索）；同一区域先巡逻再搜索时用两个不同 `job_id`。用户要求“先……再……”时在成本输入加 `"ordered": true`。多步骤或混合类型先读 [多步骤任务](combined-tasks.md)，并用 `check_task.py` 分别核对类型、参数和顺序。
- 地名：`{"place":"教学楼"}`。用户指定了形状、尺寸就一并写入，如 `{"place":"教学楼","shape":"circle","radius":5}`。
- 经纬度：`{"name":"…","center_lon":…,"center_lat":…,"shape":"circle","radius":…}` 或 `"shape":"rectangle","length":…,"width":…`。`name` 写用户对该区域的叫法，没有就写“区域1”“区域2”。
- 相对位置：按本文“相对基准”确定参照，再用 data-query 查相应记录、用 spatial-compute 的 `calc_offset.py` 算出中心。地点基准有登记区域时用区域中心 `region_center_*`，没有时用该地点的 `longitude/latitude`。偏移后的新区域从用户描述和上下文取得形状、尺寸。
- 用户指定“某机去某区域”时，在该作业里加 `"uav_id"`。
- 地名由 data-query 查询匹配，区域完整性由 spatial-compute 的 resolve_jobs.py 校验。缺项先从查询结果和上下文补齐，仍只有点位且无范围才询问形状和尺寸。

完整任务交付前读 [交付规范](plan-delivery.md)；执行或确认后下发时读 [flight-control](../../flight-control/SKILL.md)。失败时按阶段读 [诊断](diagnostics.md)，不重复完整链路。
