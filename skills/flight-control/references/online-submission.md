# 方案确认与中央下发

仅在处理待确认方案的回复，或普通任务已经授权、环境具备且准备提交时读；离线组装不预读本页。运行控制直接读 [runtime-control.md](runtime-control.md)。

## 确认卡与后续回复

**用户对确认卡的回复**（只对最近一张卡有效）：

| 回复 | 处理 |
|---|---|
| 确认（“确认”“执行”“可以”“开始”等，不带修改或疑问） | 按本技能中央下发流程执行 |
| 修改（改参数、换区域、换机、点名执行机） | 只改用户提到的部分并重算，旧卡作废；明确说“改后执行”则校验后下发，否则输出新卡并写明差异 |
| 取消、否定 | 回复“已取消，未下发任何任务” |
| 追问方案 | 按分配结果回答；卡片仍待确认，不下发 |
| 其他新请求 | 旧卡作废，按新请求处理 |

用户确认时若找不到对应方案、编号或动作序列，先从本次会话产物恢复；无法恢复才重新规划并出卡。

## 执行：中央下发四步

完整任务按 [mission-planning](../../mission-planning/SKILL.md) 判定执行授权：明确的动作指令直接进入以下流程；用户要求“确认后执行”或方案改变了明确约束时，先取得对应确认。只规划请求按 [主入口](../SKILL.md) 组装和校验，不进入在线流程。

本节依赖外部部署的 `mission_client.py`、中央服务和 ROS 环境，按实际部署配置调用。离线工作区缺少这些组件时按 [主入口](../SKILL.md) 完成离线校验 并报告未下发，不能把本地路径设成 `MIGRATION_ROOT` 来代替部署。

所有中央命令的写法：

```bash
python3 "$MIGRATION_ROOT/scripts/mission_client.py" <操作> --agent-session-id "$NANOBOT_SESSION_ID" --json '<JSON>'
```

`status` 不带 `--json`：`python3 "$MIGRATION_ROOT/scripts/mission_client.py" status --agent-session-id "$NANOBOT_SESSION_ID"`。

`request_id` 只有 register-plan、submit 和运行控制需要；authorize、preview 没有 `request_id`。

**下发前置检查（按顺序）**：

1. `mission_client.py status`：`execution_enabled` 为 true；有目标时 `perception_ready` 为 true；要跟踪时 `target_actions_ready` 为 true；参与机在 `fleet` 里 `available=true`。不满足就停止并报告。
2. 有目标时按 target-detection 为每架参与机准备感知（未运行则 start，已运行则 query），全部机共用同一个 `search_id` 和目标清单；完成后再读一次 `status`，取各机 `fleet.<uav_id>.perception`。

任一步失败：不提交；已为本任务新启动的感知，用 target-detection 的停止命令关闭；报告失败的环节和原因，不自动换方案重试。

提交后只报告已提交及各机安排，不轮询等待完成。用户看过规划再要求执行时，更新机况并取得对应原始 WGS84 序列，不能注册 dry-run 的本地坐标；需改变明确要求时才说明差异并取得接受。

**1. register-plan**（每架机一次，可并行）：

```json
{"request_id":"reg-<task_key>-<uav_id>","uav_id":"<uav_id>","batch":[原始 WGS84 动作序列],
 "perception":{"query_id":"<该机 query_id>","session_id":"<该机 session_id>"}}
```

- `batch` 使用本次组装的原始 WGS84 序列；有已确认卡时逐字复制卡片中该机的序列。
- 没有目标时省略 `perception`；有目标时取 `status` 返回的 `fleet.<uav_id>.perception`，两个值必须完全一致。
- 注册时会用与 dry-run 相同的校验器检查序列，执行模式下不必另做 dry-run。成功返回 `plan_ref`。

**2. authorize**（整个任务一次）：

```json
{"user_intent":"<用户的原始执行指令>",
 "spec":{"participants":[{"uav_id":"UAV-003","plan_ref":{"id":"plan-…","revision":1}}],
         "targets":[目标清单，没有目标时为 []],
         "policies":{"land_on_return":true},
         "authorization_ref":"pending-authorization"}}
```

`user_intent` 写真实的原始执行指令；用户另外确认过方案时追加“；用户确认：<确认原话>”，没有该回复就不添加。中央会把它和 spec 摘要一起存进授权记录。故障补做写“<原指令>；故障补做：<故障机> 未完成的 <作业名>”。取返回结果中的 `spec` 字段（已带真实 `authorization_ref`），后面两步原样使用这份 `spec`。用户要求“发现后跟踪”时，`policies` 改为：

```json
{"land_on_return":true,"on_first_confirmed":"APPROACH_AND_TRACK","tracking_target_ids":["<target_id>"],
 "approach_profile_ref":{"id":"approach-default","revision":7},"tracking_profile_ref":{"id":"tracking-default","revision":1}}
```

两个 profile 的 id 和 revision 以 `status.target_action_profiles` 的返回为准。跟踪时长、距离由 profile 固定，用户另指定时说明暂不支持修改。用户明确要求任务结束后不返航时，在 `policies` 里加 `"on_search_complete":"HOLD"`（作业完成后原地悬停，等待后续指令）。

**3. preview**：`{"spec":<authorize 返回的 spec>}`，返回 `preview_id`（60 s 内有效，只能用一次），拿到后立即 submit。

**4. submit**：

```bash
python3 "$MIGRATION_ROOT/scripts/mission_client.py" submit --agent-session-id "$NANOBOT_SESSION_ID" \
  --json '{"request_id":"submit-<task_key>","preview_id":"<preview_id>","agent_session_id":"'"$NANOBOT_SESSION_ID"'","spec":<同一份 spec>}'
```

返回 `mission_id` 和 `status=ACCEPTED`，表示任务已创建，不表示已起飞或已完成。

## 失败处理

| 错误码 | 处理 |
|---|---|
| `INVALID_PLAN`、`INVALID_INPUT` | 按 `message` 修正明确的格式或参数错误后重试一次；涉及用户明确约束或已确认动作的变化时，说明差异并重新取得接受 |
| `UAV_UNAVAILABLE`、`UAV_BUSY` | 该机不在线或被占用：停止并报告 |
| `PERCEPTION_UNAVAILABLE` | 感知引用与当前会话不一致：重新读 `status` 的 `fleet.<uav_id>.perception` 再注册 |
| `EXECUTION_DISABLED`、`ADAPTER_UNAVAILABLE`、`SERVICE_RECOVERING` | 服务当前不能执行：报告并停止 |
| `PREVIEW_EXPIRED`、`PREVIEW_CONSUMED` | 用同一份 `spec` 重新 preview，再 submit |
| `PREVIEW_CHANGED` | submit 的 `spec` 必须与 preview 完全相同 |
| `IDEMPOTENCY_CONFLICT` | 同一 `request_id` 用了不同内容：恢复原内容，或确认是新动作后换新 ID |
| 网络超时、结果不明 | 先 `get`/`events` 确认；需要重试时沿用同一 `request_id` |

## 运行控制

暂停、继续、返航、取消、跟踪控制、待决策处理和故障诊断，读 [references/runtime-control.md](runtime-control.md)。

## 注意事项

- 中央接管的机只接受中央控制：不要用 `submit_queue_batch.py` 去掉 `--dry-run` 发布，也不要用 `fleet_dispatch.py` 或直接发布 ROS 动作话题。
- `register-plan` 只接受原始 WGS84 序列；dry-run 输出（带 `_coordinates_already_mavros`）不能再提交。
- 中央计划的规则：以 `arm`、`takeoff` 开头；至少一个 `fuel_explore` 或 `patrol_move`；以 `follow_path`（可再加 `land`）结尾；`fuel_explore` 必须写 `timeout_sec`；不能用 `repeat`、`loop_forever`，多圈要逐圈展开。
- `user_intent` 中如有英文单引号要去掉，否则会破坏 `--json '…'` 的引号。
- 计划注册成功、已授权、已提交、已执行是四件不同的事，按实际返回分别报告。
