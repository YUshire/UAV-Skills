# 本地计算产物串联

`uav_runtime/.venv/bin/python uav_runtime/run.py --pipeline --input-file <本次唯一输入JSON路径>`，也可用 `--input '<JSON对象>'`。

顶层为 `steps` 数组，每步包含唯一 `id`、脚本相对路径 `script`、该脚本原有的 `input`。支持 prepare_candidates、resolve_jobs、planning_routes、allocate、assemble_plan 五个原数值脚本，按数组顺序执行。单步命令不变，不包括在线下发。

字段引用写为 `{"ref_step":"上游步骤id","select":"/data/字段"}`，只能引用本次前面成功的步骤；省略 select 时取 `/data`。程序从上游返回 JSON 读取实际 `artifact_file`，转成原有 ref/select，不搜索目录、不选择旧文件。原有明确的 ref/select 仍可用于本次已获得的输入产物。

以下示例输入已明确的区域，选择指定机，串联到完整动作；区域、机号、航高和约束均按用户任务填写，不照搬示例值。机况等参数仍按各技能核对，执行请求仍先做环境检查。

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
